RecipeGPT

RecipeGPT is an AI-powered recipe assistant that helps users discover, generate, and manage recipes through a conversational interface.

Features

- AI-powered recipe assistance through chat
- Recipe discovery and generation
- Recipe data stored in JSON and PostgreSQL
- Backend API for handling chat and recipe functionality
- React-based frontend built with Vite
- Automated tests for core functionality

Tech Stack

Frontend

- React
- Vite
- JavaScript

Backend

- Python
- FastAPI
- PostgreSQL
- Ollama for local AI model integration

Project Structure

RecipeGPT/
├── frontend/          # React + Vite frontend
├── data/              # Recipe dataset
├── src/recipegpt/     # Python package
├── assistant.py       # AI assistant logic
├── chat_api.py        # Chat API routes
├── chat_db.py         # Chat database operations
├── db.py              # Database utilities
├── main.py            # Backend application entry point
├── schema.sql         # Database schema
├── schema_chat.sql    # Chat database schema
├── tests              # Test files
└── README.md

Getting Started

Backend

1. Install Python dependencies using your preferred environment.
2. Configure the required database connection settings.
3. Ensure PostgreSQL and Ollama are available if required by your configuration.
4. Start the backend application:

python main.py

Frontend

cd frontend
npm install
npm run dev

Testing

Run the project tests with:

pytest

Status

RecipeGPT is an actively developed personal project.