# CDS X MedGemma
This is the contribution of the CDS team to the MedGemma Impact Challenge 2026.

**Team:** Team of Clinical Data Science Group, Clinic for Radiology at Ludwig-Maximilians-Universität München (LMU)

**License:** CC BY 4.0
## Quickstart
1. **Get an Openai compatible URL and API key**
2. **Configure and start:**
   ```bash
   cp code/agent-api/.env.example code/agent-api/.env
   # Edit code/agent-api/.env — paste your API key as LLM_API_KEY and EMBEDDING_API_KEY and your URL as LLM_BASE_URL and LLM_EMBEDDING_URL
   cd code
   docker compose build && docker compose up
   ```
3. **Open the app** at [http://localhost:3000](http://localhost:3000) after the application startup has completed.