# -*- coding: utf-8 -*-

import streamlit as st
import google.generativeai as genai
# ... (以下、残りのコードはそのまま)import streamlit as st
import google.generativeai as genai
import notion_client
import os
import json
import datetime

# Googleサービスアカウント認証に必要なライブラリ
from google.oauth2 import service_account
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

# --- 定数 ---
# Googleカレンダーの操作権限
GOOGLE_SCOPES = ['https://www.googleapis.com/auth/calendar']
# サービスアカウントのJSONキーファイル名 (ステップ1でDLしたもの)
# ★★★↓ ダウンロードしたJSONファイル名に書き換えてください ↓★★★
GOOGLE_SERVICE_ACCOUNT_FILE = 'gemini_key.json' 

# --- APIクライアントの初期化 ---
gemini_model = None
notion = None
gcal_service = None

# Streamlit CloudのSecretsから情報を読み込む
try:
    # --- Geminiの初期化 ---
    GEMINI_API_KEY = st.secrets.get("GEMINI_API_KEY")
    if GEMINI_API_KEY:
        genai.configure(api_key=GEMINI_API_KEY)
        gemini_model = genai.GenerativeModel('gemini-2.5-pro')
    else:
        st.warning("Gemini APIキーがStreamlit Secretsに設定されていません。")

    # --- Notionの初期化 ---
    NOTION_API_KEY = st.secrets.get("NOTION_API_KEY")
    NOTION_DB_ID = st.secrets.get("NOTION_DB_ID")
    if NOTION_API_KEY and NOTION_DB_ID:
        notion = notion_client.Client(auth=NOTION_API_KEY)
    else:
        st.warning("Notion APIキーまたはDB IDがStreamlit Secretsに設定されていません。")

    # --- Googleカレンダーの初期化 (サービスアカウント) ---
    # Streamlit Cloud (本番環境) の場合
    if "GOOGLE_CREDENTIALS_JSON_STRING" in st.secrets:
        # SecretsからJSON文字列を読み込む
        creds_json_str = st.secrets.get("GOOGLE_CREDENTIALS_JSON_STRING")
        creds_dict = json.loads(creds_json_str)
        creds = service_account.Credentials.from_service_account_info(
            creds_dict, scopes=GOOGLE_SCOPES
        )
    # ローカル環境 (テスト用) の場合
    elif os.path.exists(GOOGLE_SERVICE_ACCOUNT_FILE):
        try:
            # ★★★ 修正点: ファイルを 'utf-8' で明示的に開く ★★★
            with open(GOOGLE_SERVICE_ACCOUNT_FILE, 'r', encoding='utf-8') as f:
                creds_dict = json.load(f)

            creds = service_account.Credentials.from_service_account_info(
                creds_dict, scopes=GOOGLE_SCOPES
        )
        except Exception as e:
            st.error(f"ローカルのJSONキーファイル ({GOOGLE_SERVICE_ACCOUNT_FILE}) の読み込みに失敗しました: {e}")
            creds = None
    else:
        creds = None
        st.error(f"Googleサービスアカウントの認証情報が見つかりません。")

    if creds:
        gcal_service = build('calendar', 'v3', credentials=creds)

except Exception as e:
    st.error(f"APIクライアントの初期化中にエラーが発生しました: {e}")
    st.stop()


# --- Notion連携関数 (変更なし) ---
def add_task_to_notion(task_name):
    if not notion: return False
    try:
        notion.pages.create(
            parent={"database_id": NOTION_DB_ID},
            properties={ "名前": { "title": [ { "text": { "content": task_name } } ] } }
        )
        return True
    except Exception as e:
        st.error(f"Notionへのタスク追加に失敗: {e}")
        return False

# --- Googleカレンダー連携関数 (サービスアカウント用に変更) ---
def parse_event_with_gemini(model, text_prompt):
    if not model: return None
    now = datetime.datetime.now(datetime.timezone.utc).isoformat()
    system_prompt = f"""
    ユーザーの文章から、Googleカレンダーのイベント情報をJSON形式で抽出してください。
    - summary: イベントの概要
    - start_time: イベントの開始日時 (ISO 8601形式: YYYY-MM-DDTHH:MM:SS)
    - end_time: イベントの終了日時 (ISO 8601形式: YYYY-MM-DDTHH:MM:SS)
    ルール:
    - 現在の日時は {now} です。これを基準に「明日」「来週」などを解釈してください。
    - 終了時間が指定されていない場合、開始時間の1時間後を終了時間としてください。
    - 抽出したJSONだけを、前後の説明文なしで返してください。
    - JSONは ```json ... ``` の中に書いてください。
    """
    try:
        response = model.generate_content([system_prompt, text_prompt])
        json_text = response.text.strip().replace("```json", "").replace("```", "")
        return json.loads(json_text)
    except Exception as e:
        st.error(f"Geminiでの予定抽出に失敗: {e}\n\nGeminiの応答:\n{response.text}")
        return None

def add_event_to_calendar(service, event_details):
    if not service: return None
    try:
        event = {
            'summary': event_details['summary'],
            'start': {'dateTime': event_details['start_time'], 'timeZone': 'Asia/Tokyo'},
            'end': {'dateTime': event_details['end_time'], 'timeZone': 'Asia/Tokyo'},
        }
        # サービスアカウントは「primary」カレンダーを持たないため、
        # ステップ2で共有したカレンダーのID（通常はあなたのメールアドレス）を指定する
        calendar_owner_email = st.secrets.get("CALENDAR_OWNER_EMAIL") # Secretsから読み込む
        if not calendar_owner_email:
             st.error("カレンダー所有者のメールアドレスがSecretsに設定されていません。")
             return None

        created_event = service.events().insert(
            calendarId=calendar_owner_email, # 'primary' から変更
            body=event
        ).execute()
        return created_event.get('htmlLink')
    except HttpError as error:
        st.error(f"カレンダーへのイベント追加に失敗: {error}")
        return None

# --- メイン画面 ---
st.title("🤖 Gemini 一元管理システム (Cloud Ver)")
st.success("各APIクライアントの初期化が完了しました。")

if "messages" not in st.session_state:
    st.session_state.messages = []

for message in st.session_state.messages:
    with st.chat_message(message["role"]):
        st.markdown(message["content"])

if prompt := st.chat_input("（例: 「明日の15時にBさんとミーティング」をカレンダーに入れて）"):
    st.session_state.messages.append({"role": "user", "content": prompt})
    with st.chat_message("user"):
        st.markdown(prompt)

    with st.chat_message("assistant"):
        response_text = ""
        
        # ▼▼▼ 分岐処理 ▼▼▼
        if notion and ("notion" in prompt.lower() or "タスク" in prompt):
            st.info("Notion連携を試みています...")
            extraction_prompt = f"以下の文章からタスク名を抽出してください。\n\n原文: {prompt}"
            try:
                response = gemini_model.generate_content(extraction_prompt)
                task_name = response.text.strip().replace("`", "")
                if add_task_to_notion(task_name):
                    response_text = f"承知いたしました。タスク「{task_name}」をNotionに追加しました。"
                else:
                    response_text = "Notionへのタスク追加に失敗しました。"
            except Exception as e:
                response_text = f"Geminiでのタスク抽出中にエラーが発生: {e}"

        elif gcal_service and ("カレンダー" in prompt or "予定" in prompt):
            st.info("Googleカレンダー連携を試みています...")
            event_details = parse_event_with_gemini(gemini_model, prompt)
            if event_details:
                event_link = add_event_to_calendar(gcal_service, event_details)
                if event_link:
                    response_text = f"承知いたしました。予定「{event_details['summary']}」をGoogleカレンダーに追加しました。\n[予定を確認する]({event_link})"
                else:
                    response_text = "Googleカレンダーへの予定追加に失敗しました。"
            else:
                response_text = "予定の抽出に失敗しました。日時を明確にして再度お試しください。"

        elif gemini_model:
            try:
                response = gemini_model.generate_content(prompt)
                response_text = response.text
            except Exception as e:
                response_text = f"Geminiからの応答取得中にエラーが発生: {e}"
        else:
            response_text = "Geminiモデルが初期化されていません。"
        
        st.markdown(response_text)
        st.session_state.messages.append({"role": "assistant", "content": response_text})