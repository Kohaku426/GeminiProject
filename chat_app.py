import streamlit as st
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
GOOGLE_SERVICE_ACCOUNT_FILE = 'gemini-calendar.json' 

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
        creds = service_account.Credentials.from_service_account_file(
            GOOGLE_SERVICE_ACCOUNT_FILE, scopes=GOOGLE_SCOPES
        )
    else:
        creds = None
        st.error(f"Googleサービスアカウントの認証情報が見つかりません。")

    if creds:
        gcal_service = build('calendar', 'v3', credentials=creds)

except Exception as e:
    st.error(f"APIクライアントの初期化中にエラーが発生しました: {e}")
    st.stop()


# --- Notion Function (Modified for Due Date) ---
def add_task_to_notion(task_name, due_date=None): # Added due_date parameter
    if not notion: return False
    try:
        # ★★★ ↓↓↓ あなたのNotionの日付プロパティの名前に書き換えてください ↓↓↓ ★★★
        date_property_name = "日付" 
        # ★★★ ↑↑↑ 例: もしプロパティ名が「期日」なら "期日" にする ↑↑↑ ★★★
        
        properties_payload = {
            "名前": { "title": [ { "text": { "content": task_name } } ] }
        }
        
        # Add date property if due_date exists
        if due_date:
            try:
                # Validate date format (optional but recommended)
                datetime.datetime.strptime(due_date, '%Y-%m-%d') 
                properties_payload[date_property_name] = {
                    "date": {
                        "start": due_date # Needs YYYY-MM-DD format
                    }
                }
            except ValueError:
                st.warning(f"Invalid date format received: {due_date}. Adding task without due date.")
                
        notion.pages.create(
            parent={"database_id": NOTION_DB_ID},
            properties=properties_payload # Use updated properties
        )
        return True
    except Exception as e:
        st.error(f"Notion task failed: {e}")
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
st.title("AI HAKU via Gemini")
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

        prompt_lower = prompt.lower()
        
        # ▼▼▼ 分岐処理 ▼▼▼
        # --- Logic branches ---
        if notion and ("notion" in prompt_lower or "task" in prompt_lower): 
            st.info("Connecting to Notion...")
            # ★★★ ↓↓↓ この extraction_prompt を書き換える ↓↓↓ ★★★
            extraction_prompt = f"""
            以下の文章から、Notionに追加すべき「タスク名」と「期日」をJSON形式で抽出してください。
            - task_name: タスクの名称
            - due_date: 期日 (YYYY-MM-DD形式)。期日が指定されていない場合は null または省略してください。

            ルール:
            -**年が指定されていない場合、現在の年（2025年）を優先し、その日付が過去であれば次の年（2026年）を設定してください。
            - 現在の日時情報などを参考に、「明日」「来週末」などを具体的なYYYY-MM-DD形式に変換してください。
            - 抽出したJSONだけを、前後の説明文なしで返してください。
            - JSONは ```json ... ``` の中に書いてください。

            例1 (期日あり):
            ユーザー入力: 「牛乳を買うタスクを明日期限で追加」
            出力:
            ```json
            {{
              "task_name": "牛乳を買う",
              "due_date": "（明日の日付 YYYY-MM-DD）"
            }}
            ```

            例2 (期日なし):
            ユーザー入力: 「プレゼン資料作成をNotionタスクに」
            出力:
            ```json
            {{
              "task_name": "プレゼン資料作成",
              "due_date": null
            }}
            ```
            
            原文: {prompt}
            """
            # ★★★ ↑↑↑ ここまでが新しい extraction_prompt ↑↑↑ ★★★
            try:
                response = gemini_model.generate_content(extraction_prompt)
                
                # Extract JSON from Gemini response
                json_text = response.text.strip().replace("```json", "").replace("```", "")
                task_info = json.loads(json_text)
                
                task_name = task_info.get("task_name")
                due_date = task_info.get("due_date") # Will be None if not found

                if task_name:
                    # Pass both name and date to the function
                    if add_task_to_notion(task_name, due_date): 
                        due_date_str = f" (Due: {due_date})" if due_date else ""
                        response_text = f"OK. Added task '{task_name}'{due_date_str} to Notion."
                    else:
                        response_text = "Failed to add task to Notion."
                else:
                    response_text = "Could not extract task name from your request."
                    
            except json.JSONDecodeError:
                 st.error(f"Failed to parse JSON from Gemini.\nGemini response:\n{response.text}")
                 response_text = "Error parsing task details from Gemini."
            except Exception as e:
                response_text = f"Gemini task extraction failed: {e}"

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

        elif "email" in prompt_lower or "mail" in prompt_lower:
            st.info("Analyzing email body for task/event creation...")
    
            # Use the new comprehensive parser
            parsed_info = parse_email_with_gemini(gemini_model, prompt)
    
            if not parsed_info:
                response_text = "Failed to extract structured data from the email."
    
            # --- Action Dispatch ---
            elif parsed_info.get("action") == "event":
                # Prepare data for Calendar function (requires specific keys)
                event_details = {
                    "summary": parsed_info.get("summary"),
                    "start_time": parsed_info.get("start_time"),
                    "end_time": parsed_info.get("end_time"),
                }
                event_link = add_event_to_calendar(gcal_service, event_details)
                if event_link:
                    response_text = f"Successfully added event '{event_details['summary']}' to Google Calendar. \n[View Event]({event_link})"
                else:
                    response_text = "Failed to add event to Google Calendar."
            
            elif parsed_info.get("action") == "task" and parsed_info.get("summary"):
                # Prepare data for Notion function (requires specific keys)
                task_name = parsed_info.get("summary")
                due_date = parsed_info.get("date")
        
                if add_task_to_notion(task_name, due_date):
                    due_date_str = f" (Due: {due_date})" if due_date else ""
                    response_text = f"Successfully added task '{task_name}'{due_date_str} to Notion."
                else:
                    response_text = "Failed to add task to Notion."

            else:
                response_text = "Email analysis complete, but no clear event or task was identified."

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

# --- New: Parse Email for both Task and Calendar Event ---
def parse_email_with_gemini(model, email_body):
    if not model: return None
    now = datetime.datetime.now(datetime.timezone.utc).isoformat()

    # ★★★ IMPORTANT: This prompt asks Gemini to return a specific JSON structure ★★★
    system_prompt = f"""
    The user will provide an email body. Extract the main task/event and its details into a single JSON object. 
    
    Current Date/Time: {now}
    
    Required JSON Structure (Return only this JSON object):
    {{
      "action": "task" or "event" (Choose "event" if a specific date/time is mentioned, otherwise choose "task"),
      "summary": "Main subject or task description",
      "date": "YYYY-MM-DD" or null (Required if action is "task"),
      "start_time": "YYYY-MM-DDTHH:MM:SS" or null (Required if action is "event"),
      "end_time": "YYYY-MM-DDTHH:MM:SS" or null (Required if action is "event"; default 1 hour later if only start is present)
    }}

    Rules:
    - If a specific day/time is found (e.g., 'Meeting tomorrow at 10 AM'), set action to "event".
    - If only a general chore is found (e.g., 'Please follow up on the report'), set action to "task".
    - Use {now} to interpret relative dates.
    - Respond ONLY with the JSON object, inside ```json ... ```.
    """
    
    try:
        response = model.generate_content([system_prompt, email_body])
        json_text = response.text.strip().replace("```json", "").replace("```", "")
        return json.loads(json_text)
    except Exception as e:
        st.error(f"Gemini email parsing failed: {e}\n\nGemini response:\n{response.text}")
        return None
