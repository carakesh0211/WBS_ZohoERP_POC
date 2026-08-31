"""FastAPI routers for the production application.

Each module here exports a plain `router = APIRouter()` and owns no part of
`app/backend/main.py` -- mounting is the Runtime agent's responsibility, so
that route wiring, middleware and startup ordering stay in one place.
"""
