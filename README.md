# Aunty Next Door - Call Transcription & Google Sheets Sync

A powerful Streamlit-based web application that automates call transcription, quality control (QC) analysis using Groq AI, and sequential synchronization with Google Sheets.

---

## Features
* **User Authentication:** Secure SQLite-backed login and user management system.
* **AI-Powered QC & Transcription:** Integrates with Groq models to transcribe audio and run targeted quality assurance questions.
* **On-Demand Google Sheets Sync:** Processes columns (such as H and J) in real-time directly through the Streamlit interface.
* **Cloud Ready:** Fully configured for deployment on Streamlit Community Cloud.

---

## Project Structure
```text
├── .github/
│   └── workflows/          # GitHub Actions workflows
├── .gitignore              # Files ignored by Git
├── packages.txt            # System dependencies (e.g., ffmpeg for audio)
├── requirements.txt        # Python dependencies
├── streamlit.app.py        # Main Streamlit application script
└── README.md               # Project documentation
