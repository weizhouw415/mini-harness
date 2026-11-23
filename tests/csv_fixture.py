import csv
import io
import secrets
from urllib.parse import parse_qs

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, PlainTextResponse, RedirectResponse


LOGIN = """<!doctype html>
<html lang="en">
<title>Inventory sign in</title>
<h1>Inventory sign in</h1>
<form method="post" action="login">
  <label>Username <input id="username" name="username"></label>
  <label>Password <input id="password" name="password" type="password"></label>
  <button id="login">Sign in</button>
</form>
"""

INVENTORY = """<!doctype html>
<html lang="en">
<title>Inventory</title>
<h1>Inventory</h1>
<a id="download" href="download">Download inventory CSV</a>
<form id="upload-form">
  <label>Processed inventory <input id="upload" type="file" required accept=".csv"></label>
  <button id="submit">Upload</button>
</form>
<p id="receipt" role="status"></p>
<script>
  document.querySelector('#upload-form').addEventListener('submit', async (event) => {
    event.preventDefault();
    const uploaded = document.querySelector('#upload').files[0];
    const response = await fetch('upload', { method: 'POST', body: uploaded });
    document.querySelector('#receipt').textContent = await response.text();
  });
</script>
"""


class CsvFixture:
    """A local acceptance site with deliberately fake credentials and exact upload checks."""

    def __init__(self, rows: tuple[tuple[str, int], ...] = (("apples", 2), ("pears", 5))):
        self.source_rows = rows
        self.expected_rows = [["item", "quantity"], *[[item, str(quantity * 2)] for item, quantity in rows]]
        self.uploads: list[list[list[str]]] = []
        self.login_count = 0
        self.cookie = secrets.token_urlsafe(24)
        self.app = FastAPI()
        self.app.get("/")(self.home)
        self.app.post("/login")(self.login)
        self.app.get("/download")(self.download)
        self.app.post("/upload")(self.upload)

    def signed_in(self, request: Request) -> bool:
        return secrets.compare_digest(request.cookies.get("fixture-session", ""), self.cookie)

    async def home(self, request: Request):
        return HTMLResponse(INVENTORY if self.signed_in(request) else LOGIN)

    async def login(self, request: Request):
        fields = parse_qs((await request.body()).decode("utf-8"))
        if fields != {"username": ["fixture-user"], "password": ["fixture-password"]}:
            return PlainTextResponse("Invalid fixture credentials", status_code=401)
        self.login_count += 1
        response = RedirectResponse("./", status_code=303)
        response.set_cookie("fixture-session", self.cookie, httponly=True, samesite="strict")
        return response

    async def download(self, request: Request):
        if not self.signed_in(request):
            return PlainTextResponse("Sign in required", status_code=401)
        output = io.StringIO()
        writer = csv.writer(output)
        writer.writerow(["item", "quantity"])
        writer.writerows(self.source_rows)
        return PlainTextResponse(
            output.getvalue(),
            media_type="text/csv",
            headers={"Content-Disposition": 'attachment; filename="source.csv"'},
        )

    async def upload(self, request: Request):
        if not self.signed_in(request):
            return PlainTextResponse("Sign in required", status_code=401)
        try:
            rows = list(csv.reader(io.StringIO((await request.body()).decode("utf-8")), strict=True))
        except (csv.Error, UnicodeDecodeError):
            return PlainTextResponse("Invalid CSV", status_code=422)
        if rows != self.expected_rows:
            return PlainTextResponse("Unexpected inventory", status_code=422)
        self.uploads.append(rows)
        return PlainTextResponse("Inventory accepted")
