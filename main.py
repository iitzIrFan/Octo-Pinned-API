from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
import httpx
import os
from dotenv import load_dotenv

# Load .env variables
load_dotenv()

app = FastAPI()

# Define allowed origins BEFORE using them
allowed_origins = os.getenv("ALLOWED_ORIGINS", "").split(",")

# GitHub Configuration
GITHUB_TOKEN = os.getenv("GITHUB_TOKEN")
GITHUB_USERNAME = os.getenv("GITHUB_USERNAME")

HEADERS = {
    "Authorization": f"Bearer {GITHUB_TOKEN}"
}

# CORS Middleware
app.add_middleware(
    CORSMiddleware,
    allow_origins=allowed_origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# GraphQL query to get pinned repositories
GRAPHQL_QUERY = f"""
query {{
  user(login: "{GITHUB_USERNAME}") {{
    pinnedItems(first: 6, types: [REPOSITORY]) {{
      nodes {{
        ... on Repository {{
          name
          description
          url
          stargazerCount
          forkCount
          languages(first: 1) {{
            nodes {{
              name
            }}
          }}
        }}
      }}
    }}
  }}
}}
"""

# Using FastAPIs decorator to create a GET endpoint at /api/pinned
@app.get("/api/pinned")
async def get_pinned():
    try:
        # request code
        async with httpx.AsyncClient() as client:
            response = await client.post(
                'https://api.github.com/graphql',
                json={"query": GRAPHQL_QUERY},
                headers=HEADERS
            )
            response.raise_for_status()
            data = response.json()
            return data["data"]["user"]["pinnedItems"]["nodes"]
    except Exception as e:
        return {"error": str(e)}