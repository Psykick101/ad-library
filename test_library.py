"""Ad Library folders, tags and sources (no network). Run: python3 test_library.py"""
import io
import json
import os
import pathlib
import sys
import tempfile

T = pathlib.Path(tempfile.mkdtemp())
(T / "key").write_text("k")
os.environ.update(LIBRARY_DB=str(T / "lib.db"), LIBRARY_MEDIA=str(T / "media"), LIBRARY_KEY=str(T / "key"),
                  FOREPLAY_ENV=str(T / "fp.env"))
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
import library_service as L  # noqa: E402
from PIL import Image  # noqa: E402
from starlette.testclient import TestClient  # noqa: E402

L.ai_read = lambda aid: None
L._scan = lambda p: None
c = TestClient(L.app)
H = {"X-Library-Key": "k", "X-User": "dana@x.com"}
ok = n = 0


def case(name, cond):
    global ok, n
    n += 1
    ok += bool(cond)
    print(("PASS " if cond else "FAIL ") + name)


def png(col):
    b = io.BytesIO()
    Image.new("RGB", (40, 40), col).save(b, "PNG")
    return b.getvalue()


case("needs the key", c.get("/v1/folders").status_code == 403)
f1 = c.post("/v1/folders", headers=H, json={"name": "Winners Q4"}).json()["folder"]
case("make a folder", f1["name"] == "Winners Q4")
case("same name = same folder", c.post("/v1/folders", headers=H, json={"name": "winners q4"}).json()["folder"]["id"] == f1["id"])
a = c.post("/v1/save", headers=H, files={"file": ("a.png", png("red"), "image/png")},
           data={"source": "extension", "tags": "UGC, Hook ,ugc", "folder": f1["id"]}).json()["id"]
b = c.post("/v1/save", headers=H, files={"file": ("b.png", png("blue"), "image/png")}, data={"source": "instagram-dm"}).json()["id"]
L.q("insert into ads values(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", ("fp1", 1, "sync", "foreplay", "Foreplay", "", "", "image", "x", "",
                                                                   "", "", "[]", "", "", "F1", "{}"))
lib = c.get("/v1/items", headers=H).json()
case("default = our library only (extension + phone bots), no Foreplay", {i["id"] for i in lib["items"]} == {a, b} and lib["total"] == 2)
case("Foreplay tab = Foreplay only", [i["id"] for i in c.get("/v1/items?source=foreplay", headers=H).json()["items"]] == ["fp1"])
case("show all = everything", len(c.get("/v1/items?source=all", headers=H).json()["items"]) == 3)
case("saved straight into a folder + clean tags", c.get(f"/v1/items?folder={f1['id']}", headers=H).json()["items"][0]["tags"] == ["ugc", "hook"])
case("unfiled filter", [i["id"] for i in c.get("/v1/items?folder=none", headers=H).json()["items"]] == [b])
c.post(f"/v1/folders/{f1['id']}/items", headers=H, json={"ids": [b]})
case("drop into a folder", len(c.get(f"/v1/items?folder={f1['id']}", headers=H).json()["items"]) == 2)
c.post("/v1/tags", headers=H, json={"ids": [a, b], "add": ["Summer"], "remove": ["hook"]})
case("label many at once", [i["id"] for i in c.get("/v1/items?tag=summer", headers=H).json()["items"]] != []
     and "hook" not in json.dumps(c.get("/v1/items?tag=ugc", headers=H).json()["items"][0]["tags"]))
fl = c.get("/v1/folders", headers=H).json()
case("folders + counts + tag list", fl["folders"][0]["n"] == 2 and fl["tags"][0] == {"tag": "summer", "n": 2})
c.post(f"/v1/folders/{f1['id']}/items", headers=H, json={"ids": [b], "remove": True})
case("take out of a folder", len(c.get(f"/v1/items?folder={f1['id']}", headers=H).json()["items"]) == 1)
c.patch(f"/v1/folders/{f1['id']}", headers=H, json={"name": "Best of Q4"})
c.delete(f"/v1/folders/{f1['id']}", headers=H)
case("delete folder keeps the saves", c.get("/v1/folders", headers=H).json()["folders"] == [] and
     len(c.get("/v1/items", headers=H).json()["items"]) == 2)
case("can't delete someone else's save", c.delete(f"/v1/items/{a}", headers={**H, "X-User": "eli@x.com"}).status_code == 403)
case("admin can", c.delete(f"/v1/items/{a}", headers={**H, "X-User": "boss@x.com", "X-Admin": "1"}).json()["ok"])
case("own save can be deleted", c.delete(f"/v1/items/{b}", headers=H).json()["ok"] and c.get("/v1/items", headers=H).json()["total"] == 0)
print(f"{ok}/{n} OK")
sys.exit(0 if ok == n else 1)
