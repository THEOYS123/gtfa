#!/usr/bin/env python3

import os
import sys
import json
import time
import base64
import mimetypes
import webbrowser
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

try:
    import requests
except Exception:
    print("Missing dependency: requests\nInstall: pip install requests")
    sys.exit(1)

try:
    from rich.console import Console
    from rich.table import Table
    from rich.prompt import Prompt, Confirm
    from rich.progress import Progress, SpinnerColumn, BarColumn, TimeElapsedColumn
    from rich.panel import Panel
    from rich.markdown import Markdown
    from rich.text import Text
    from rich.box import ROUNDED
    from rich.traceback import install as install_traceback
except Exception:
    print("Missing dependency: rich\nInstall: pip install rich")
    sys.exit(1)

try:
    from watchdog.observers import Observer
    from watchdog.events import FileSystemEventHandler, FileSystemEvent
except Exception:
    print("Missing dependency: watchdog\nInstall: pip install watchdog")
    sys.exit(1)

install_traceback()
console = Console()
CONFIG_PATH = Path.home() / ".gh_upload_tool.json"
GITHUB_API = "https://api.github.com"

def load_config() -> Dict[str, Any]:
    if CONFIG_PATH.exists():
        try:
            return json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
        except Exception:
            return {}
    return {}

def save_config(cfg: Dict[str, Any]) -> None:
    CONFIG_PATH.write_text(json.dumps(cfg, indent=2), encoding="utf-8")

def ensure_config(cfg: Dict[str, Any]) -> Dict[str, Any]:
    defaults = {
        "token": "",
        "owner": "",
        "repo": "",
        "branch": "main",
        "pages_branch": "gh-pages",
        "auto_commit_message": "Auto upload via gtfa Tool",
        "sync_ignore": [".git", "__pycache__"]
    }
    for k, v in defaults.items():
        cfg.setdefault(k, v)
    return cfg

def prompt_initial_cfg(cfg: Dict[str, Any]) -> Dict[str, Any]:
    console.print(Panel("[bold yellow]Initial setup / invalid token[/bold yellow]\nProvide a GitHub Personal Access Token (PAT) with Contents: Read & Write"))
    token = Prompt.ask("GitHub PAT", default=cfg.get("token", ""))
    owner = Prompt.ask("Owner / Username", default=cfg.get("owner", ""))
    repo = Prompt.ask("Repo name", default=cfg.get("repo", ""))
    branch = Prompt.ask("Default branch", default=cfg.get("branch", "main"))
    pages_branch = Prompt.ask("Pages branch (default gh-pages)", default=cfg.get("pages_branch", "gh-pages"))
    auto_msg = Prompt.ask("Default commit message", default=cfg.get("auto_commit_message", "Auto upload via gtfa Tool"))
    new = {
        "token": token.strip(),
        "owner": owner.strip(),
        "repo": repo.strip(),
        "branch": branch.strip(),
        "pages_branch": pages_branch.strip(),
        "auto_commit_message": auto_msg.strip(),
        "sync_ignore": cfg.get("sync_ignore", [".git", "__pycache__"])
    }
    save_config(new)
    return new

def api_request(method: str, endpoint: str, token: Optional[str], **kwargs) -> requests.Response:
    url = GITHUB_API + endpoint
    headers = kwargs.pop("headers", {})
    if token:
        headers.setdefault("Authorization", f"token {token}")
    headers.setdefault("Accept", "application/vnd.github.v3+json")
    r = requests.request(method, url, headers=headers, **kwargs)
    return r

def test_auth(token: str) -> Tuple[bool, Optional[str]]:
    if not token:
        return False, None
    r = api_request("GET", "/user", token)
    if r.status_code == 200:
        return True, r.json().get("login")
    return False, None

def list_user_repos(token: str, per_page:int=100, max_pages:int=5) -> Tuple[bool, Any]:
    repos = []
    page = 1
    while True:
        r = api_request("GET", f"/user/repos?per_page={per_page}&page={page}", token)
        if r.status_code != 200:
            return False, r.json() if r.content else {"message": f"HTTP {r.status_code}"}
        data = r.json()
        if not data:
            break
        repos.extend(data)
        if len(data) < per_page or page >= max_pages:
            break
        page += 1
    return True, repos

def get_repo_contents(token: str, owner: str, repo: str, path: str = "", branch: str = "main") -> Tuple[bool, Any]:
    endpoint = f"/repos/{owner}/{repo}/contents/{path}" if path else f"/repos/{owner}/{repo}/contents"
    r = api_request("GET", endpoint + f"?ref={branch}", token)
    if r.status_code == 200:
        return True, r.json()
    try:
        return False, r.json()
    except Exception:
        return False, {"message": f"HTTP {r.status_code}"}

def get_file_sha(token: str, owner: str, repo: str, path: str, branch: str = "main") -> Optional[str]:
    ok, data = get_repo_contents(token, owner, repo, path, branch)
    if ok and isinstance(data, dict):
        return data.get("sha")
    return None

def create_or_update_file(token: str, owner: str, repo: str, path: str, content_b64: str, message: str, branch: str = "main", sha: Optional[str] = None) -> Tuple[bool, Any]:
    endpoint = f"/repos/{owner}/{repo}/contents/{path}"
    payload = {"message": message, "content": content_b64, "branch": branch}
    if sha:
        payload["sha"] = sha
    r = api_request("PUT", endpoint, token, json=payload)
    return (r.status_code in (200, 201)), (r.json() if r.content else {"status": r.status_code})

def delete_file(token: str, owner: str, repo: str, path: str, message: str, branch: str = "main", sha: Optional[str] = None) -> Tuple[bool, Any]:
    endpoint = f"/repos/{owner}/{repo}/contents/{path}"
    payload = {"message": message, "branch": branch}
    if sha:
        payload["sha"] = sha
    r = api_request("DELETE", endpoint, token, json=payload)
    return (r.status_code == 200), (r.json() if r.content else {"status": r.status_code})

def download_file_contents(token: str, owner: str, repo: str, path: str, branch: str = "main") -> Tuple[bool, Optional[bytes]]:
    ok, data = get_repo_contents(token, owner, repo, path, branch)
    if ok and isinstance(data, dict) and data.get("content"):
        try:
            raw = base64.b64decode(data["content"])
            return True, raw
        except Exception:
            return False, None
    return False, None

def get_ref(token: str, owner: str, repo: str, branch: str) -> Tuple[bool, Any]:
    r = api_request("GET", f"/repos/{owner}/{repo}/git/ref/heads/{branch}", token)
    if r.status_code == 200:
        return True, r.json()
    try:
        return False, r.json()
    except Exception:
        return False, {"message": f"HTTP {r.status_code}"}

def create_blob(token: str, owner: str, repo: str, content_b64: str, encoding: str = "base64") -> Tuple[bool, Any]:
    r = api_request("POST", f"/repos/{owner}/{repo}/git/blobs", token, json={"content": content_b64, "encoding": encoding})
    if r.status_code in (201,):
        return True, r.json()
    return False, (r.json() if r.content else {"status": r.status_code})

def create_tree(token: str, owner: str, repo: str, tree: List[Dict[str, Any]], base_tree: Optional[str] = None) -> Tuple[bool, Any]:
    payload = {"tree": tree}
    if base_tree:
        payload["base_tree"] = base_tree
    r = api_request("POST", f"/repos/{owner}/{repo}/git/trees", token, json=payload)
    if r.status_code in (201,):
        return True, r.json()
    return False, (r.json() if r.content else {"status": r.status_code})

def create_commit(token: str, owner: str, repo: str, message: str, tree_sha: str, parents: List[str]) -> Tuple[bool, Any]:
    payload = {"message": message, "tree": tree_sha, "parents": parents}
    r = api_request("POST", f"/repos/{owner}/{repo}/git/commits", token, json=payload)
    if r.status_code in (201,):
        return True, r.json()
    return False, (r.json() if r.content else {"status": r.status_code})

def update_ref(token: str, owner: str, repo: str, branch: str, commit_sha: str, force: bool = False) -> Tuple[bool, Any]:
    payload = {"sha": commit_sha, "force": force}
    r = api_request("PATCH", f"/repos/{owner}/{repo}/git/refs/heads/{branch}", token, json=payload)
    if r.status_code in (200,):
        return True, r.json()
    return False, (r.json() if r.content else {"status": r.status_code})

def get_pages(token: str, owner: str, repo: str) -> Tuple[bool, Any]:
    r = api_request("GET", f"/repos/{owner}/{repo}/pages", token)
    if r.status_code == 200:
        return True, r.json()
    try:
        return False, r.json()
    except Exception:
        return False, {"message": f"HTTP {r.status_code}"}

def create_or_update_pages(token: str, owner: str, repo: str, cfg: Dict[str, Any]) -> Tuple[bool, Any]:
    r = api_request("PUT", f"/repos/{owner}/{repo}/pages", token, json=cfg)
    if r.status_code in (200, 201):
        return True, r.json()
    return False, (r.json() if r.content else {"status": r.status_code})

def delete_pages_api(token: str, owner: str, repo: str) -> Tuple[bool, Any]:
    r = api_request("DELETE", f"/repos/{owner}/{repo}/pages", token)
    return (r.status_code == 204), (r.json() if r.content else {"status": r.status_code})

def rebuild_pages(token: str, owner: str, repo: str) -> Tuple[bool, Any]:
    r = api_request("POST", f"/repos/{owner}/{repo}/pages/builds", token)
    return (r.status_code in (201, 202)), (r.json() if r.content else {"status": r.status_code})

def file_to_base64(path: Path) -> str:
    return base64.b64encode(path.read_bytes()).decode("utf-8")

def gather_files_for_folder(folder: Path, skip_patterns: Optional[List[str]] = None) -> List[Path]:
    skip_patterns = skip_patterns or []
    files = []
    for p in folder.rglob("*"):
        if p.is_file() and not any(part in p.parts for part in skip_patterns):
            files.append(p)
    return files

def path_to_repo_path(local_base: Path, file_path: Path, repo_base: str = "") -> str:
    rel = file_path.relative_to(local_base)
    parts = [p for p in rel.parts if p not in (".", "..")]
    if repo_base:
        return "/".join([repo_base.strip("/")] + parts)
    return "/".join(parts)

def show_header(cfg: Dict[str, Any]) -> None:
    md = Markdown(f"# GitHub Tools full akses By  Flood | ngoprek.xyz/contact — v1.0 \n**Repo:** {cfg.get('owner')}/{cfg.get('repo')}  •  **Branch:** {cfg.get('branch')}")
    console.print(md)

def op_list(cfg: Dict[str, Any]) -> None:
    token, owner, repo, branch = cfg["token"], cfg["owner"], cfg["repo"], cfg["branch"]
    path = Prompt.ask("Masukkan path repo untuk dilihat (kosong = root)", default="")
    ok, data = get_repo_contents(token, owner, repo, path, branch)
    if not ok:
        console.print(f"[red]Gagal mengambil daftar: {data}[/red]")
        return
    table = Table(show_header=True, header_style="bold magenta")
    table.add_column("Name")
    table.add_column("Type")
    table.add_column("Size", justify="right")
    table.add_column("Path")
    if isinstance(data, dict):
        table.add_row(data.get("name", "-"), data.get("type", "-"), str(data.get("size", 0)), data.get("path", "-"))
    else:
        for item in data:
            table.add_row(item.get("name", "-"), item.get("type", "-"), str(item.get("size", 0)), item.get("path", "-"))
    console.print(table)

def op_upload_file(cfg: Dict[str, Any]) -> None:
    token, owner, repo, branch = cfg["token"], cfg["owner"], cfg["repo"], cfg["branch"]
    local_path = Path(Prompt.ask("Local file path (contoh: ./file.txt)")).expanduser()
    if not local_path.exists() or not local_path.is_file():
        console.print("[red]File tidak ditemukan.[/red]")
        return
    repo_path_default = local_path.name
    repo_path = Prompt.ask("Target path in repo (contoh: folder/file.txt)", default=repo_path_default)
    sha = get_file_sha(token, owner, repo, repo_path, branch)
    if sha:
        console.print(f"[yellow]File sudah ada di repo (sha: {sha}).[/yellow]")
        if not Confirm.ask("Overwrite file ini?"):
            console.print("[cyan]Batal overwrite.[/cyan]")
            return
    content_b64 = file_to_base64(local_path)
    message = Prompt.ask("Commit message", default=cfg.get("auto_commit_message"))
    ok, resp = create_or_update_file(token, owner, repo, repo_path, content_b64, message, branch, sha)
    if ok:
        console.print(f"[green]Berhasil upload {local_path} -> {repo_path}[/green]")
    else:
        console.print(f"[red]Gagal upload: {resp}[/red]")

def op_upload_folder(cfg: Dict[str, Any]) -> None:
    token, owner, repo, branch = cfg["token"], cfg["owner"], cfg["repo"], cfg["branch"]
    local_folder = Path(Prompt.ask("Local folder path (contoh: ./myfolder)")).expanduser()
    if not local_folder.exists() or not local_folder.is_dir():
        console.print("[red]Folder tidak ditemukan.[/red]")
        return
    mode = Prompt.ask("Upload mode: [1] per-file (safer) [2] single-commit batch (clean history)", choices=["1","2"], default="2")
    target_repo_base = Prompt.ask("Target repo folder (kosong = root)", default="")
    files = gather_files_for_folder(local_folder)
    if not files:
        console.print("[yellow]Tidak ada file didalam folder.[/yellow]")
        return
    message = Prompt.ask("Commit message for this batch", default=cfg.get("auto_commit_message"))
    console.print(Panel(f"Mulai upload {len(files)} file dari {local_folder} -> {repo}/{target_repo_base or '/'} on branch {branch} (mode {mode})"))
    if mode == "1":
        with Progress(SpinnerColumn(), "[progress.description]{task.description}", BarColumn(), "[progress.percentage]{task.percentage:>3.0f}", TimeElapsedColumn(), console=console) as prog:
            task = prog.add_task("Uploading...", total=len(files))
            successes = 0
            failures = []
            for f in files:
                repo_path = path_to_repo_path(local_folder, f, repo_base=target_repo_base)
                sha = get_file_sha(token, owner, repo, repo_path, branch)
                content_b64 = file_to_base64(f)
                ok, resp = create_or_update_file(token, owner, repo, repo_path, content_b64, message, branch, sha)
                if ok:
                    successes += 1
                else:
                    failures.append((str(f), resp))
                prog.advance(task)
        console.print(f"[green]Selesai. Berhasil: {successes}. Gagal: {len(failures)}[/green]")
        if failures:
            console.print("[red]List failures:[/red]")
            for f, r in failures:
                console.print(f"- {f}: {r}")
    else:
        console.print("[cyan]Building blobs and tree for single commit...[/cyan]")
        tree_entries = []
        failures = []
        with Progress(SpinnerColumn(), "[progress.description]{task.description}", BarColumn(), "[progress.percentage]{task.percentage:>3.0f}", TimeElapsedColumn(), console=console) as prog:
            task = prog.add_task("Creating blobs...", total=len(files))
            for f in files:
                try:
                    bytes_data = f.read_bytes()
                    b64 = base64.b64encode(bytes_data).decode("utf-8")
                    ok_blob, resp_blob = create_blob(token, owner, repo, b64, encoding="base64")
                    if not ok_blob:
                        failures.append((str(f), resp_blob))
                        prog.advance(task)
                        continue
                    blob_sha = resp_blob.get("sha")
                    repo_path = path_to_repo_path(local_folder, f, repo_base=target_repo_base)
                    tree_entries.append({"path": repo_path, "mode": "100644", "type": "blob", "sha": blob_sha})
                except Exception as e:
                    failures.append((str(f), str(e)))
                prog.advance(task)
        if failures:
            console.print(f"[red]Beberapa blob gagal dibuat: {len(failures)}. Batal commit batch.[/red]")
            for p, r in failures:
                console.print(f"- {p}: {r}")
            return
        ok_ref, ref_data = get_ref(token, owner, repo, branch)
        if not ok_ref:
            console.print(f"[red]Gagal ambil ref branch {branch}: {ref_data}[/red]")
            return
        base_commit_sha = ref_data["object"]["sha"]
        rcommit = api_request("GET", f"/repos/{owner}/{repo}/git/commits/{base_commit_sha}", token)
        if rcommit.status_code != 200:
            console.print(f"[red]Gagal ambil commit object: {rcommit.status_code} {rcommit.text}[/red]")
            return
        base_tree_sha = rcommit.json()["tree"]["sha"]
        ok_tree, resp_tree = create_tree(token, owner, repo, tree_entries, base_tree=base_tree_sha)
        if not ok_tree:
            console.print(f"[red]Gagal membuat tree: {resp_tree}[/red]")
            return
        new_tree_sha = resp_tree.get("sha")
        ok_commit, resp_commit = create_commit(token, owner, repo, message, new_tree_sha, parents=[base_commit_sha])
        if not ok_commit:
            console.print(f"[red]Gagal membuat commit: {resp_commit}[/red]")
            return
        new_commit_sha = resp_commit.get("sha")
        ok_update, resp_update = update_ref(token, owner, repo, branch, new_commit_sha, force=False)
        if not ok_update:
            console.print(f"[red]Gagal update branch ref: {resp_update}[/red]")
            return
        console.print(f"[green]Batch upload selesai. Commit: {new_commit_sha}[/green]")

def op_delete(cfg: Dict[str, Any]) -> None:
    token, owner, repo, branch = cfg["token"], cfg["owner"], cfg["repo"], cfg["branch"]
    target = Prompt.ask("Masukkan path file/folder di repo yang ingin dihapus (folder: path/ to list files first)").strip()
    if not target:
        console.print("[red]Path kosong.[/red]")
        return
    ok, data = get_repo_contents(token, owner, repo, target, branch)
    if not ok:
        console.print(f"[red]Gagal mengakses path: {data}[/red]")
        return
    if isinstance(data, dict) and data.get("type") == "file":
        sha = data.get("sha")
        console.print(f"[yellow]File detected: {data.get('path')} size={data.get('size')}[/yellow]")
        if Confirm.ask("Yakin hapus file ini?"):
            msg = Prompt.ask("Commit message", default=f"Delete {target} via GitHub Upload Tool")
            ok2, resp = delete_file(token, owner, repo, target, msg, branch, sha)
            if ok2:
                console.print(f"[green]File {target} berhasil dihapus.[/green]")
            else:
                console.print(f"[red]Gagal hapus: {resp}[/red]")
    else:
        console.print(f"[yellow]Directory detected. Gathering files under {target}...[/yellow]")
        file_paths: List[str] = []
        def gather_rec(pth):
            ok2, dat = get_repo_contents(token, owner, repo, pth, branch)
            if not ok2:
                return
            if isinstance(dat, dict) and dat.get("type") == "file":
                file_paths.append(dat.get("path"))
            elif isinstance(dat, list):
                for it in dat:
                    if it.get("type") == "file":
                        file_paths.append(it.get("path"))
                    elif it.get("type") == "dir":
                        gather_rec(it.get("path"))
        gather_rec(target)
        total_files = len(file_paths)
        console.print(f"[red]Akan menghapus {total_files} file di bawah '{target}' secara rekursif.[/red]")
        if total_files == 0:
            console.print("[yellow]Tidak ada file untuk dihapus.[/yellow]")
            return
        if not Confirm.ask("Yakin ingin menghapus semua file ini?"):
            console.print("[cyan]Dibatalkan.[/cyan]")
            return
        msg = Prompt.ask("Commit message", default=f"Delete folder {target} via GitHub Upload Tool")
        failures = []
        with Progress(SpinnerColumn(), "[progress.description]{task.description}", BarColumn(), "[progress.percentage]{task.percentage:>3.0f}", TimeElapsedColumn(), console=console) as prog:
            task = prog.add_task("Deleting...", total=total_files)
            for p in file_paths:
                sha = get_file_sha(token, owner, repo, p, branch)
                ok2, resp = delete_file(token, owner, repo, p, msg, branch, sha)
                if not ok2:
                    failures.append((p, resp))
                prog.advance(task)
        console.print(f"[green]Selesai. Gagal: {len(failures)}[/green]")
        if failures:
            for p, r in failures:
                console.print(f"- {p}: {r}")

def op_download(cfg: Dict[str, Any]) -> None:
    token, owner, repo, branch = cfg["token"], cfg["owner"], cfg["repo"], cfg["branch"]
    target = Prompt.ask("File path in repo to download (contoh: folder/file.txt)").strip()
    if not target:
        console.print("[red]Path kosong.[/red]")
        return
    ok, data = get_repo_contents(token, owner, repo, target, branch)
    if not ok:
        console.print(f"[red]Gagal mengambil file: {data}[/red]")
        return
    if isinstance(data, dict) and data.get("type") == "file":
        raw_ok, raw = download_file_contents(token, owner, repo, target, branch)
        if not raw_ok:
            console.print("[red]Gagal decode content.[/red]")
            return
        local_path = Path(Prompt.ask("Simpan sebagai (local path)", default=os.path.basename(target))).expanduser()
        try:
            local_path.parent.mkdir(parents=True, exist_ok=True)
            local_path.write_bytes(raw)
            console.print(f"[green]File tersimpan: {local_path}[/green]")
        except Exception as e:
            console.print(f"[red]Gagal menulis file: {e}[/red]")
    else:
        console.print("[red]Target bukan file atau tidak ditemukan.[/red]")

def op_rename(cfg: Dict[str, Any]) -> None:
    token, owner, repo, branch = cfg["token"], cfg["owner"], cfg["repo"], cfg["branch"]
    old = Prompt.ask("Old path in repo (contoh: folder/old.txt)").strip()
    if not old:
        console.print("[red]Path lama kosong.[/red]")
        return
    ok, data = get_repo_contents(token, owner, repo, old, branch)
    if not ok:
        console.print(f"[red]Gagal mengakses: {data}[/red]")
        return
    if isinstance(data, dict) and data.get("type") == "file":
        new = Prompt.ask("New path in repo (contoh: folder/new.txt)").strip()
        if not new:
            console.print("[red]Path baru kosong.[/red]")
            return
        raw_ok, raw = download_file_contents(token, owner, repo, old, branch)
        if not raw_ok:
            console.print("[red]Gagal baca file lama.[/red]")
            return
        content_b64 = base64.b64encode(raw).decode("utf-8")
        message_create = Prompt.ask("Commit message for rename (create)", default=f"Rename create {new}")
        sha_new = get_file_sha(token, owner, repo, new, branch)
        ok2, resp2 = create_or_update_file(token, owner, repo, new, content_b64, message_create, branch, sha_new)
        if not ok2:
            console.print(f"[red]Gagal membuat file baru: {resp2}[/red]")
            return
        sha_old = data.get("sha")
        message_delete = Prompt.ask("Commit message for delete old", default=f"Rename delete {old}")
        ok3, resp3 = delete_file(token, owner, repo, old, message_delete, branch, sha_old)
        if ok3:
            console.print(f"[green]Rename berhasil: {old} -> {new}[/green]")
        else:
            console.print(f"[yellow]Buat file baru berhasil tetapi gagal hapus file lama: {resp3}[/yellow]")
    else:
        console.print("[red]Path lama bukan file atau tidak ditemukan.[/red]")

def ensure_pages_branch(cfg: Dict[str, Any], branch_name: str) -> bool:
    token, owner, repo = cfg["token"], cfg["owner"], cfg["repo"]
    ok, ref = get_ref(token, owner, repo, branch_name)
    if ok:
        return True
    console.print(f"[yellow]Branch '{branch_name}' belum ada. Mencoba buat dari '{cfg.get('branch','main')}'...[/yellow]")
    base_branch = cfg.get("branch", "main")
    ok2, refdata = get_ref(token, owner, repo, base_branch)
    if not ok2:
        console.print(f"[red]Base branch {base_branch} tidak ditemukan: {refdata}[/red]")
        return False
    base_sha = refdata["object"]["sha"]
    r = api_request("POST", f"/repos/{owner}/{repo}/git/refs", cfg["token"], json={"ref": f"refs/heads/{branch_name}", "sha": base_sha})
    if r.status_code in (201,):
        console.print(f"[green]Branch '{branch_name}' berhasil dibuat.[/green]")
        return True
    console.print(f"[red]Gagal membuat branch '{branch_name}': {r.status_code} {r.text}[/red]")
    return False

def pages_create_auto(cfg: Dict[str, Any]) -> None:
    token, owner, repo = cfg["token"], cfg["owner"], cfg["repo"]
    pages_branch = cfg.get("pages_branch", "gh-pages")
    if not ensure_pages_branch(cfg, pages_branch):
        return
    index_html = f"""<!doctype html>
<html><head><meta charset="utf-8"><title>Welcome</title></head>
<body style="font-family: sans-serif; text-align:center; padding:3rem;">
<h1>Welcome to your new GitHub Pages 🚀</h1>
<p>Generated by GitHub Tools full akses  — Flood | <a href="ngoprek.xyz/contact">Contact owner</a></a> — v1.0 — PREMIUM v1.0</p>
<p>Repo: {owner}/{repo}</p>
</body></html>"""
    content_b64 = base64.b64encode(index_html.encode("utf-8")).decode("utf-8")
    sha = get_file_sha(token, owner, repo, "index.html", pages_branch)
    msg = Prompt.ask("Commit message for index.html", default=f"Create GitHub Pages index for {repo}")
    ok1, resp1 = create_or_update_file(token, owner, repo, "index.html", content_b64, msg, pages_branch, sha)
    if not ok1:
        console.print(f"[red]Gagal upload index.html ke branch {pages_branch}: {resp1}[/red]")
        return
    cfg_pages = {"source": {"branch": pages_branch, "path": "/"}}
    ok2, resp2 = create_or_update_pages(token, owner, repo, cfg_pages)
    if ok2:
        console.print(f"[green]GitHub Pages diaktifkan (branch: {pages_branch}).[/green]")
        ok_status, data = get_pages(token, owner, repo)
        if ok_status:
            console.print(f"[cyan]URL: {data.get('html_url')}[/cyan]")
    else:
        console.print(f"[red]Gagal mengaktifkan Pages: {resp2}[/red]")

def pages_create_manual(cfg: Dict[str, Any]) -> None:
    token, owner, repo = cfg["token"], cfg["owner"], cfg["repo"]
    pages_branch = Prompt.ask("Branch untuk Pages (default gh-pages)", default=cfg.get("pages_branch", "gh-pages"))
    pages_path = Prompt.ask("Folder source di branch (default /)", default="/")
    custom_domain = Prompt.ask("Custom domain (kosong = none)", default="")
    if not ensure_pages_branch(cfg, pages_branch):
        return
    if custom_domain.strip():
        cname_content = custom_domain.strip() + "\n"
        c_b64 = base64.b64encode(cname_content.encode("utf-8")).decode("utf-8")
        sha = get_file_sha(token, owner, repo, "CNAME", pages_branch)
        msg = Prompt.ask("Commit message for CNAME", default=f"Add CNAME for pages {custom_domain}")
        okc, rc = create_or_update_file(token, owner, repo, "CNAME", c_b64, msg, pages_branch, sha)
        if not okc:
            console.print(f"[red]Gagal membuat CNAME: {rc}[/red]")
            return
    payload = {"source": {"branch": pages_branch, "path": pages_path}}
    if custom_domain.strip():
        payload["cname"] = custom_domain.strip()
    ok, resp = create_or_update_pages(token, owner, repo, payload)
    if ok:
        console.print(f"[green]Pages berhasil dikonfigurasi (branch: {pages_branch}, path: {pages_path}).[/green]")
        s, d = get_pages(token, owner, repo)
        if s:
            console.print(f"[cyan]URL: {d.get('html_url')}[/cyan]")
    else:
        console.print(f"[red]Gagal konfigurasi Pages: {resp}[/red]")

def pages_edit_file(cfg: Dict[str, Any]) -> None:
    token, owner, repo = cfg["token"], cfg["owner"], cfg["repo"]
    pages_branch = Prompt.ask("Branch Pages to edit (default gh-pages)", default=cfg.get("pages_branch", "gh-pages"))
    okb, _ = get_ref(token, owner, repo, pages_branch)
    if not okb:
        console.print(f"[red]Branch {pages_branch} tidak ditemukan. Jalankan buat Pages dulu.[/red]")
        return
    path = Prompt.ask("Path file in pages branch (contoh: index.html)").strip()
    ok, data = get_repo_contents(token, owner, repo, path, pages_branch)
    if not ok:
        console.print(f"[red]Gagal ambil file: {data}[/red]")
        return
    if not isinstance(data, dict) or data.get("type") != "file":
        console.print("[red]Target bukan file.[/red]")
        return
    raw_ok, raw = download_file_contents(token, owner, repo, path, pages_branch)
    if not raw_ok:
        console.print("[red]Gagal decode isi file.[/red]")
        return
    current = raw.decode("utf-8", errors="replace")
    console.print(Panel("Current content preview (first 400 chars):\n\n" + current[:400] + ("\n\n[...truncated]" if len(current) > 400 else "")))
    if not Confirm.ask("Edit file ini?"):
        console.print("[cyan]Dibatalkan[/cyan]")
        return
    console.print("[green]Masukkan isi baru. Untuk multiline paste, akhiri input dengan baris berisi hanya: __END__[/green]")
    lines = []
    while True:
        ln = Prompt.ask("")
        if ln.strip() == "__END__":
            break
        lines.append(ln)
    new_content = "\n".join(lines).strip()
    if not new_content:
        console.print("[red]Tidak ada perubahan. Dibatalkan.[/red]")
        return
    backup_path = f".backup/{path}.bak"
    b_b64 = base64.b64encode(current.encode("utf-8")).decode("utf-8")
    bmsg = f"Backup {path} before edit via GitHub Upload Tool"
    create_or_update_file(token, owner, repo, backup_path, b_b64, bmsg, pages_branch, get_file_sha(token, owner, repo, backup_path, pages_branch))
    content_b64 = base64.b64encode(new_content.encode("utf-8")).decode("utf-8")
    msg = Prompt.ask("Commit message for edit", default=f"Edit {path} in Pages")
    ok2, r2 = create_or_update_file(token, owner, repo, path, content_b64, msg, pages_branch, data.get("sha"))
    if ok2:
        console.print(f"[green]File {path} updated in branch {pages_branch}.[/green]")
    else:
        console.print(f"[red]Gagal update file: {r2}[/red]")

def pages_add_file_or_folder(cfg: Dict[str, Any]) -> None:
    token, owner, repo = cfg["token"], cfg["owner"], cfg["repo"]
    pages_branch = Prompt.ask("Target Pages branch (default gh-pages)", default=cfg.get("pages_branch", "gh-pages"))
    if not ensure_pages_branch(cfg, pages_branch):
        return
    choice = Prompt.ask("Tambah (1) File atau (2) Folder ?", choices=["1", "2"], default="1")
    if choice == "1":
        local_path = Path(Prompt.ask("Local file path")).expanduser()
        if not local_path.exists() or not local_path.is_file():
            console.print("[red]File lokal tidak ditemukan.[/red]")
            return
        repo_path = Prompt.ask("Target path in pages branch (contoh: assets/img.png)", default=local_path.name)
        content_b64 = file_to_base64(local_path)
        sha = get_file_sha(token, owner, repo, repo_path, pages_branch)
        msg = Prompt.ask("Commit message", default=f"Add {repo_path} to Pages")
        ok, r = create_or_update_file(token, owner, repo, repo_path, content_b64, msg, pages_branch, sha)
        if ok:
            console.print(f"[green]File {repo_path} uploaded to {pages_branch}.[/green]")
        else:
            console.print(f"[red]Gagal upload: {r}[/red]")
    else:
        local_folder = Path(Prompt.ask("Local folder path")).expanduser()
        if not local_folder.exists() or not local_folder.is_dir():
            console.print("[red]Folder lokal tidak ditemukan.[/red]")
            return
        target_repo_base = Prompt.ask("Target folder in pages branch (kosong = root)", default="")
        files = gather_files_for_folder(local_folder)
        if not files:
            console.print("[yellow]Tidak ada file di folder lokal.[/yellow]")
            return
        msg = Prompt.ask("Commit message for batch", default=f"Add folder {local_folder.name} to Pages")
        with Progress(SpinnerColumn(), "[progress.description]{task.description}", BarColumn(), "[progress.percentage]{task.percentage:>3.0f}", TimeElapsedColumn(), console=console) as prog:
            task = prog.add_task("Uploading...", total=len(files))
            fails = []
            for f in files:
                repo_path = path_to_repo_path(local_folder, f, repo_base=target_repo_base)
                content_b64 = file_to_base64(f)
                sha = get_file_sha(token, owner, repo, repo_path, pages_branch)
                ok, r = create_or_update_file(token, owner, repo, repo_path, content_b64, msg, pages_branch, sha)
                if not ok:
                    fails.append((str(f), r))
                prog.advance(task)
        console.print(f"[green]Selesai. Gagal: {len(fails)}[/green]")
        if fails:
            for f, r in fails:
                console.print(f"- {f}: {r}")

def pages_view_status(cfg: Dict[str, Any]) -> None:
    token, owner, repo = cfg["token"], cfg["owner"], cfg["repo"]
    ok, data = get_pages(token, owner, repo)
    if not ok:
        console.print(f"[red]Tidak dapat mengambil status Pages: {data}[/red]")
        return
    table = Table(show_header=False, box=ROUNDED)
    table.add_column("Key")
    table.add_column("Value")
    table.add_row("URL", str(data.get("html_url")))
    src = data.get("source", {})
    table.add_row("Source branch", f"{src.get('branch')}:{src.get('path')}")
    table.add_row("Status", str(data.get("status")))
    table.add_row("CNAME", str(data.get("cname")))
    table.add_row("Public", str(data.get("public", True)))
    console.print(table)

def pages_rebuild(cfg: Dict[str, Any]) -> None:
    token, owner, repo = cfg["token"], cfg["owner"], cfg["repo"]
    ok, resp = rebuild_pages(token, owner, repo)
    if ok:
        console.print("[green]Rebuild Pages started.[/green]")
    else:
        console.print(f"[red]Gagal trigger rebuild: {resp}[/red]")

def pages_delete(cfg: Dict[str, Any]) -> None:
    token, owner, repo = cfg["token"], cfg["owner"], cfg["repo"]
    console.print(Panel("Hapus GitHub Pages - akan menghapus konfigurasi Pages"))
    if not Confirm.ask("Yakin hapus konfigurasi GitHub Pages?"):
        console.print("[cyan]Dibatalkan[/cyan]")
        return
    ok, resp = delete_pages_api(token, owner, repo)
    if ok:
        console.print("[green]Pages configuration deleted.[/green]")
    else:
        console.print(f"[red]Gagal delete Pages: {resp}[/red]")
    if Confirm.ask("Juga hapus branch gh-pages? (opsional)"):
        gh = cfg.get("pages_branch", "gh-pages")
        r = api_request("DELETE", f"/repos/{owner}/{repo}/git/refs/heads/{gh}", token)
        if r.status_code in (204,):
            console.print(f"[green]Branch {gh} deleted.[/green]")
        else:
            console.print(f"[yellow]Gagal hapus branch {gh}: {r.status_code} {r.text}[/yellow]")

def dev_preview_pages(cfg: Dict[str, Any]) -> None:
    token, owner, repo = cfg["token"], cfg["owner"], cfg["repo"]
    ok, data = get_pages(token, owner, repo)
    if not ok:
        console.print(f"[red]Tidak dapat mengambil status Pages: {data}[/red]")
        return
    url = data.get("html_url")
    console.print(f"[cyan]Pages URL: {url}[/cyan]")
    if Confirm.ask("Open in browser?"):
        webbrowser.open(url)
    if Confirm.ask("Fetch HTML preview and show first 800 chars?"):
        try:
            r = requests.get(url, timeout=10)
            if r.status_code == 200:
                preview = r.text[:800]
                console.print(Panel(preview + ("\n\n[...truncated]" if len(r.text) > 800 else "")))
            else:
                console.print(f"[yellow]Page fetch returned status: {r.status_code}[/yellow]")
        except Exception as e:
            console.print(f"[red]Error fetching page: {e}[/red]")

def dev_backup_pages(cfg: Dict[str, Any]) -> None:
    token, owner, repo = cfg["token"], cfg["owner"], cfg["repo"]
    local_backup_root = Path(Prompt.ask("Local backup folder", default=f"./{owner}_{repo}_pages_backup")).expanduser()
    pages_branch = cfg.get("pages_branch", "gh-pages")
    console.print(f"[cyan]Backing up pages from branch '{pages_branch}' to local folder: {local_backup_root}[/cyan]")
    local_backup_root.mkdir(parents=True, exist_ok=True)
    file_paths = []
    def gather(pth):
        ok, dat = get_repo_contents(token, owner, repo, pth, pages_branch)
        if not ok:
            return
        if isinstance(dat, dict) and dat.get("type") == "file":
            file_paths.append(dat.get("path"))
        elif isinstance(dat, list):
            for it in dat:
                if it.get("type") == "file":
                    file_paths.append(it.get("path"))
                elif it.get("type") == "dir":
                    gather(it.get("path"))
    gather("")
    if not file_paths:
        console.print("[yellow]No files to backup in Pages branch.[/yellow]")
        return
    with Progress(SpinnerColumn(), "[progress.description]{task.description}", BarColumn(), "[progress.percentage]{task.percentage:>3.0f}", TimeElapsedColumn(), console=console) as prog:
        task = prog.add_task("Downloading...", total=len(file_paths))
        for p in file_paths:
            raw_ok, raw = download_file_contents(token, owner, repo, p, pages_branch)
            if raw_ok and raw is not None:
                local_path = local_backup_root / p
                local_path.parent.mkdir(parents=True, exist_ok=True)
                local_path.write_bytes(raw)
            else:
                console.print(f"[red]Failed to download: {p}[/red]")
            prog.advance(task)
    console.print(f"[green]Backup completed to {local_backup_root}[/green]")

class SyncEventHandler(FileSystemEventHandler):
    def __init__(self, cfg: Dict[str, Any], local_root: Path, target_repo_base: str, pages_branch: str):
        super().__init__()
        self.cfg = cfg
        self.token = cfg["token"]
        self.owner = cfg["owner"]
        self.repo = cfg["repo"]
        self.local_root = local_root
        self.target_repo_base = target_repo_base.strip("/")
        self.pages_branch = pages_branch
        self.ignore = cfg.get("sync_ignore", [])

    def _is_ignored(self, path: Path) -> bool:
        return any(part in path.parts for part in self.ignore)

    def _repo_path(self, src_path: Path) -> str:
        rel = src_path.relative_to(self.local_root)
        repo_path = "/".join(rel.parts)
        if self.target_repo_base:
            return f"{self.target_repo_base}/{repo_path}"
        return repo_path

    def on_created(self, event: FileSystemEvent):
        if event.is_directory: return
        src = Path(event.src_path)
        if self._is_ignored(src): return
        console.print(f"[green]Detected created: {src} -> uploading...[/green]")
        try:
            content_b64 = file_to_base64(src)
            repo_path = self._repo_path(src)
            sha = get_file_sha(self.token, self.owner, self.repo, repo_path, self.pages_branch)
            ok, resp = create_or_update_file(self.token, self.owner, self.repo, repo_path, content_b64, f"Auto-sync add {repo_path}", self.pages_branch, sha)
            if ok:
                console.print(f"[green]Uploaded {repo_path}[/green]")
            else:
                console.print(f"[red]Upload failed: {resp}[/red]")
        except Exception as e:
            console.print(f"[red]Error on created: {e}[/red]")

    def on_modified(self, event: FileSystemEvent):
        if event.is_directory: return
        src = Path(event.src_path)
        if self._is_ignored(src): return
        console.print(f"[yellow]Detected modified: {src} -> uploading...[/yellow]")
        try:
            content_b64 = file_to_base64(src)
            repo_path = self._repo_path(src)
            sha = get_file_sha(self.token, self.owner, self.repo, repo_path, self.pages_branch)
            ok, resp = create_or_update_file(self.token, self.owner, self.repo, repo_path, content_b64, f"Auto-sync update {repo_path}", self.pages_branch, sha)
            if ok:
                console.print(f"[green]Updated {repo_path}[/green]")
            else:
                console.print(f"[red]Update failed: {resp}[/red]")
        except Exception as e:
            console.print(f"[red]Error on modified: {e}[/red]")

    def on_deleted(self, event: FileSystemEvent):
        if event.is_directory: return
        src = Path(event.src_path)
        if self._is_ignored(src): return
        console.print(f"[red]Detected deleted: {src} -> deleting from repo...[/red]")
        try:
            repo_path = self._repo_path(src)
            sha = get_file_sha(self.token, self.owner, self.repo, repo_path, self.pages_branch)
            if not sha:
                console.print(f"[yellow]File not found in repo: {repo_path}[/yellow]")
                return
            ok, resp = delete_file(self.token, self.owner, self.repo, repo_path, f"Auto-sync delete {repo_path}", self.pages_branch, sha)
            if ok:
                console.print(f"[green]Deleted {repo_path} from repo[/green]")
            else:
                console.print(f"[red]Delete failed: {resp}[/red]")
        except Exception as e:
            console.print(f"[red]Error on deleted: {e}[/red]")

def dev_auto_sync(cfg: Dict[str, Any]) -> None:
    local = Path(Prompt.ask("Local folder to watch (will sync changes)", default="./")).expanduser()
    if not local.exists() or not local.is_dir():
        console.print("[red]Local folder not found[/red]")
        return
    pages_branch = Prompt.ask("Target branch to sync (default gh-pages)", default=cfg.get("pages_branch","gh-pages"))
    target_repo_base = Prompt.ask("Target path in branch (empty = root)", default="")
    console.print(Panel(f"Auto-sync started\nLocal: {local}\n-> Repo: {cfg['owner']}/{cfg['repo']} branch: {pages_branch} target: {target_repo_base}\nPress Ctrl+C to stop"))
    event_handler = SyncEventHandler(cfg, local, target_repo_base, pages_branch)
    observer = Observer()
    observer.schedule(event_handler, str(local), recursive=True)
    observer.start()
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        console.print("\n[cyan]Stopping auto-sync...[/cyan]")
        observer.stop()
    observer.join()
    console.print("[green]Auto-sync stopped[/green]")

def op_switch_repo(cfg: Dict[str, Any]) -> None:
    token = cfg["token"]
    owner_current = cfg.get("owner", "")
    console.print(Panel("Switch Repo — Quick Mode"))
    mode = Prompt.ask("Mode: (1) Type owner/repo or repo (2) Pick from your repos list", choices=["1", "2"], default="2")
    chosen_owner = None
    chosen_repo = None
    if mode == "1":
        text = Prompt.ask("Input (owner/repo or repo)").strip()
        if "/" in text:
            chosen_owner, chosen_repo = text.split("/", 1)
        else:
            chosen_repo = text
            chosen_owner = owner_current or Prompt.ask("Owner (was empty)", default=owner_current or "")
    else:
        with console.status("[cyan]Fetching your repos...[/cyan]", spinner="dots"):
            ok, data = list_user_repos(token)
        if not ok:
            console.print(f"[red]Failed fetch repos: {data}[/red]")
            return
        repos = data
        if not repos:
            console.print("[yellow]No repos found for this user[/yellow]")
            return
        table = Table(show_header=True, header_style="bold magenta")
        table.add_column("#", width=4)
        table.add_column("Full name")
        table.add_column("Private", justify="center")
        for i, r in enumerate(repos, 1):
            table.add_row(str(i), r.get("full_name"), str(r.get("private")))
        console.print(table)
        idx = Prompt.ask(f"Pick repo number (1-{len(repos)}) or 0 to cancel", default="0")
        try:
            ni = int(idx)
            if ni <= 0:
                console.print("[cyan]Cancelled[/cyan]")
                return
            sel = repos[ni - 1]
            chosen_owner = sel.get("owner", {}).get("login")
            chosen_repo = sel.get("name")
        except Exception:
            console.print("[red]Invalid choice[/red]")
            return
    if not chosen_owner or not chosen_repo:
        console.print("[red]Owner or repo missing[/red]")
        return
    console.print(f"[yellow]Switching to {chosen_owner}/{chosen_repo}...[/yellow]")
    ok, data = get_repo_contents(cfg["token"], chosen_owner, chosen_repo, "", cfg.get("branch", "main"))
    if not ok:
        console.print(f"[red]Cannot access repo: {data}[/red]")
        return
    cfg["owner"] = chosen_owner
    cfg["repo"] = chosen_repo
    save_config(cfg)
    console.print(f"[green]Switched active repo -> {chosen_owner}/{chosen_repo}[/green]")

def op_change_cfg(cfg: Dict[str, Any]) -> Dict[str, Any]:
    console.print(Panel("Ganti konfigurasi repo/token/branch"))
    token = Prompt.ask("GitHub Personal Access Token (kosong = pakai token current)", default=cfg.get("token", ""))
    owner = Prompt.ask("Owner / Username", default=cfg.get("owner", ""))
    repo = Prompt.ask("Repo name", default=cfg.get("repo", ""))
    branch = Prompt.ask("Branch", default=cfg.get("branch", "main"))
    pages_branch = Prompt.ask("Pages branch (default gh-pages)", default=cfg.get("pages_branch", "gh-pages"))
    auto_msg = Prompt.ask("Default commit message", default=cfg.get("auto_commit_message", "Auto upload via GitHub Upload Tool"))
    new_cfg = {
        "token": token.strip(),
        "owner": owner.strip(),
        "repo": repo.strip(),
        "branch": branch.strip(),
        "pages_branch": pages_branch.strip(),
        "auto_commit_message": auto_msg.strip(),
        "sync_ignore": cfg.get("sync_ignore", [".git", "__pycache__"])
    }
    save_config(new_cfg)
    console.print("[green]Config tersimpan.[/green]")
    return new_cfg

def main_menu_loop():
    cfg = load_config()
    cfg = ensure_config(cfg)
    if not cfg.get("token") or not cfg.get("owner") or not cfg.get("repo"):
        cfg = prompt_initial_cfg(cfg)
    ok, login = test_auth(cfg["token"])
    if not ok:
        console.print("[red]Token tidak valid atau tidak ada akses. Silakan perbarui token.[/red]")
        cfg = prompt_initial_cfg(cfg)
        ok, login = test_auth(cfg["token"])
        if not ok:
            console.print("[red]Token masih invalid. Keluar.[/red]")
            sys.exit(1)
    console.print(f"[green]Authenticated as {login}[/green]")

    while True:
        console.clear()
        show_header(cfg)
        console.print(Panel(Text(
            "[1] Upload file\n[2] Upload folder\n[3] Hapus file/folder\n[4] Lihat isi repo (ls)\n[5] Download file\n[6] Rename file/folder\n[7] Ganti repo / token\n[8] Switch Repo Quick\n[9] Kelola GitHub Pages\n[10] Dev Tools (Preview / Backup / Auto-sync)\n[0] Keluar",
            justify="left"
        ), title="=== Menu ==="))
        choice = Prompt.ask("Pilih nomor", choices=[str(i) for i in range(0, 11)], default="0")
        if choice == "1":
            op_upload_file(cfg)
        elif choice == "2":
            op_upload_folder(cfg)
        elif choice == "3":
            op_delete(cfg)
        elif choice == "4":
            op_list(cfg)
        elif choice == "5":
            op_download(cfg)
        elif choice == "6":
            op_rename(cfg)
        elif choice == "7":
            cfg = op_change_cfg(cfg)
        elif choice == "8":
            op_switch_repo(cfg)
        elif choice == "9":
            while True:
                console.clear()
                console.print(Panel(f"[bold]GitHub Pages — {cfg.get('owner')}/{cfg.get('repo')}[/bold]", style="cyan"))
                console.print(Panel("[1] Buat GitHub Pages (otomatis/manual)\n[2] Edit file di GitHub Pages\n[3] Tambah file/folder ke GitHub Pages\n[4] Lihat status GitHub Pages\n[5] Rebuild / Deploy ulang GitHub Pages\n[6] Hapus GitHub Pages\n[0] Kembali ke menu utama", title="Pages Menu"))
                ch = Prompt.ask("Pilih", choices=[str(i) for i in range(0, 7)], default="0")
                if ch == "1":
                    sub = Prompt.ask("Mode: [1] Otomatis (rekomendasi) [2] Manual (advanced)", choices=["1", "2"], default="1")
                    if sub == "1":
                        pages_create_auto(cfg)
                    else:
                        pages_create_manual(cfg)
                elif ch == "2":
                    pages_edit_file(cfg)
                elif ch == "3":
                    pages_add_file_or_folder(cfg)
                elif ch == "4":
                    pages_view_status(cfg)
                elif ch == "5":
                    pages_rebuild(cfg)
                elif ch == "6":
                    pages_delete(cfg)
                elif ch == "0":
                    break
                _ = Prompt.ask("\nTekan enter untuk kembali ke Pages menu")
        elif choice == "10":
            while True:
                console.clear()
                console.print(Panel("[1] Preview Pages\n[2] Backup Pages to local\n[3] Auto-sync local folder -> repo branch/path (watch)\n[0] Back", title="Dev Tools"))
                ch = Prompt.ask("Pilih", choices=["0","1","2","3"], default="0")
                if ch == "1":
                    dev_preview_pages(cfg)
                elif ch == "2":
                    dev_backup_pages(cfg)
                elif ch == "3":
                    dev_auto_sync(cfg)
                elif ch == "0":
                    break
                _ = Prompt.ask("\nTekan enter untuk kembali ke Dev Tools menu")
        elif choice == "0":
            console.print("[cyan]Sampai jumpa![/cyan]")
            break
        else:
            console.print("[red]Pilihan tidak dikenal[/red]")
        _ = Prompt.ask("\nTekan enter untuk kembali ke menu")

if __name__ == "__main__":
    try:
        main_menu_loop()
    except KeyboardInterrupt:
        console.print("\n[cyan]Dibatalkan oleh user.[/cyan]")
        sys.exit(0)
