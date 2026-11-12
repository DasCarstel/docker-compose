import asyncio
import sys

sys.path.insert(0, "/app")

from app.api.auth import create_access_token
from app.database import async_session_maker
from sqlalchemy import text

USER = "831d6447-a6cd-4bbb-a5cc-c1479ad549a8"


async def main():
    async with async_session_maker() as s:
        row = (await s.execute(text("select id from user_identities where user_id=:u limit 1"), {"u": USER})).first()
        ident = str(row.id)
    token = create_access_token(USER, ident)
    import httpx
    async with httpx.AsyncClient(timeout=45) as c:
        r = await c.post(
            "https://wardrobe.mueller-nas.de/api/v1/outfits/suggest-options",
            json={"occasion": "casual", "count": 3},
            headers={"Authorization": f"Bearer {token}"},
        )
        print("HTTP", r.status_code)
        if r.status_code >= 400:
            print(r.text[:300])


asyncio.run(main())
