# -*- coding: utf-8 -*-
import streamlit as st
import google.generativeai as genai
import notion_client
import os
import json
import datetime

# --- Import Google Libs ---
from google.oauth2 import service_account
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

# --- Constants ---
GOOGLE_SCOPES = ['https://www.googleapis.com/auth/calendar']
# --- IMPORTANT: Change this to your actual service account JSON file name ---
GOOGLE_SERVICE_ACCOUNT_FILE = 'gemini_key.json' 

# --- API Client Initialization ---
gemini_model = None
notion = None
gcal_service = None

try:
    # --- Init Gemini ---
    GEMINI_API_KEY = st.secrets.get("GEMINI_API_KEY")
    if GEMINI_API_KEY:
        genai.configure(api_key=GEMINI_API_KEY)
        gemini_model = genai.GenerativeModel('gemini-2.5-pro')
    else:
        st.warning("Gemini API key is not set in Streamlit Secrets.")

    # --- Init Notion ---
    NOTION_API_KEY = st.secrets.get("NOTION_API_KEY")
    NOTION_DB_ID = st.secrets.get("NOTION_DB_ID")
    if NOTION_API_KEY and NOTION_DB_ID:
        notion = notion_client.Client(auth=NOTION_API_KEY)
    else:
        st.warning("Notion API key or DB ID is not set in Streamlit Secrets.")

    # --- Init Google Calendar (Service Account) ---
    creds = None
    # On Streamlit Cloud
    if "GOOGLE_CREDENTIALS_JSON_STRING" in st.secrets:
        creds_json_str = st.secrets.get("GOOGLE_CREDENTIALS_JSON_STRING")
        creds_dict = json.loads(creds_json_str)
        creds = service_account.Credentials.from_service_account_info(
            creds_dict, scopes=GOOGLE_SCOPES
        )
    # On Local (for testing)
    elif os.path.exists(GOOGLE_SERVICE_ACCOUNT_FILE):
        try:
            with open(GOOGLE_SERVICE_ACCOUNT_FILE, 'r', encoding='utf-8') as f:
                creds_dict = json.load(f)
            creds = service_account.Credentials.from_service_account_info(
                creds_dict, scopes=GOOGLE_SCOPES
            )
        except Exception as e:
            st.error(f"Failed to load local JSON key ({GOOGLE_SERVICE_ACCOUNT_FILE}): {e}")
    else:
        st.error("Google Service Account credentials not found.")

    if creds:
        gcal_service = build('calendar', 'v3', credentials=creds)

except Exception as e:
    st.error(f"Error during API client initialization: {e}")
    st.stop()


# --- Notion Function ---
def add_task_to_notion(task_name):
    if not notion: return False
    try:
        notion.pages.create(
            parent={"database_id": NOTION_DB_ID},
            properties={ "名前": { "title": [ { "text": { "content": task_name } } ] } }
        )
        return True
    except Exception as e:
        st.error(f"Notion task failed: {e}")
        return False

# --- Google Calendar Functions ---
def parse_event_with_gemini(model, text_prompt):
    if not model: return None
    now = datetime.datetime.now(datetime.timezone.utc).isoformat()
    system_prompt = f"""
    Extract Google Calendar event info from the user's text as JSON:
    - summary: Event title
    - start_time: Event start time (ISO 8601: YYYY-MM-DDTHH:MM:SS)
    - end_time: Event end time (ISO 8601: YYYY-MM-DDTHH:MM:SS)
    Rules:
    - Current time is {now}. Use this to interpret "tomorrow", "next week", etc.
    - If no end time, assume 1 hour duration.
    - Respond ONLY with the JSON, inside ```json ... ```.
    """
    try:
        response = model.generate_content([system_prompt, text_prompt])
        json_text = response.text.strip().replace("```json", "").replace("```", "")
        return json.loads(json_text)
    except Exception as e:
        st.error(f"Gemini parse failed: {e}\n\nGemini response:\n{response.text}")
        return None

def add_event_to_calendar(service, event_details):
    if not service: return None
    try:
        CALENDAR_OWNER_EMAIL = st.secrets.get("CALENDAR_OWNER_EMAIL")
        if not CALENDAR_OWNER_EMAIL:
             st.error("CALENDAR_OWNER_EMAIL is not set in Secrets.")
             return None

        event = {
            'summary': event_details['summary'],
            'start': {'dateTime': event_details['start_time'], 'timeZone': 'Asia/Tokyo'},
            'end': {'dateTime': event_details['end_time'], 'timeZone': 'Asia/Tokyo'},
        }
        created_event = service.events().insert(
            calendarId=CALENDAR_OWNER_EMAIL,
            body=event
        ).execute()
        return created_event.get('htmlLink')
    except HttpError as error:
        st.error(f"GCal add event failed: {error}")
        return None

# --- Main App ---
st.title("🤖 Gemini Control Hub (Cloud Ver)")
st.success("API clients initialized.")

if "messages" not in st.session_state:
    st.session_state.messages = []

for message in st.session_state.messages:
    with st.chat_message(message["role"]):
        st.markdown(message["content"])

if prompt := st.chat_input("Input command (e.g., 'Add task to Notion' or 'Add event to calendar')"):
    st.session_state.messages.append({"role": "user", "content": prompt})
    with st.chat_message("user"):
        st.markdown(prompt)

    with st.chat_message("assistant"):
        response_text = ""

        # --- Logic branches ---
        if notion and ("notion" in prompt.lower() or "タスク" in prompt): # "タスク" is Japanese, but it's in a string, which is fine
            st.info("Connecting to Notion...")
            extraction_prompt = f"Extract task name from: {prompt}"
            try:
                response = gemini_model.generate_content(extraction_prompt)
                task_name = response.text.strip().replace("`", "")
                if add_task_to_notion(task_name):
                    response_text = f"OK. Added task '{task_name}' to Notion."
                else:
                    response_text = "Failed to add task to Notion."
            except Exception as e:
                response_text = f"Gemini task extraction failed: {e}"

        elif gcal_service and ("カレンダー" in prompt or "予定" in prompt or "calendar" in prompt or "schedule" in prompt):
            st.info("Connecting to Google Calendar...")
            event_details = parse_event_with_gemini(gemini_model, prompt)
            if event_details:
                event_link = add_event_to_calendar(gcal_service, event_details)
                if event_link:
                    response_text = f"OK. Added event '{event_details['summary']}' to Google Calendar. \n[View Event]({event_link})"
                else:
                    response_text = "Failed to add event to Google Calendar."
            else:
                response_text = "Failed to parse event. Please be more specific about the date and time."

        elif gemini_model:
            try:
                response = gemini_model.generate_content(prompt)
                response_text = response.text
            except Exception as e:
                response_text = f"Gemini response error: {e}"
        else:
            response_text = "Error: Gemini model is not initialized."

        st.markdown(response_text)
        st.session_state.messages.append({"role": "assistant", "content": response_text})