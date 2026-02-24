# TURBO - Tumor Board Assistant

This is the contribution of the CDS team to the MedGemma Impact Challenge 2026. 

**Team:** Clinical Data Science in Radiology, Department of Radiology, LMU University Hospital, LMU Munich
**License:** CC BY 4.0

## Video Introduction
[![TURBO - Tumor Board Assistant | CDS | Kaggle Submission - MedGemma Impact Challenge ](https://img.youtube.com/vi/ZHto0XCb6ao/sddefault.jpg)](https://www.youtube.com/watch?v=ZHto0XCb6ao)


## Quickstart
1. **Get an Openai compatible URL and API key**
2. **Configure and start:**
   ```bash
   cp code/agent-api/.env.example code/agent-api/.env
   # Edit code/agent-api/.env — paste your API key as LLM_API_KEY and EMBEDDING_API_KEY
   # and your URL as LLM_BASE_URL and LLM_EMBEDDING_URL as well as your embedding model.
   cd code
   docker compose build && docker compose up
   ```
3. **Open the app** at [http://localhost:3000](http://localhost:3000) after the application startup has completed.
