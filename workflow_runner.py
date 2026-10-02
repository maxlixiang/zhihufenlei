"""External orchestration only: existing collectors, classifiers and schemas stay intact."""
from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
from urllib.parse import unquote, urlparse
import uuid

import yaml


class ConsoleProgress:
    """仅显示进度；静默期间定时输出最近操作，退出时停止提示线程。"""

    def __init__(self, interval: float = 5.0):
        self.interval = interval
        self.message = ""
        self.started = time.monotonic()
        self.last_output = self.started
        self.lock = threading.Lock()
        self.stop = threading.Event()
        self.thread = threading.Thread(target=self._heartbeat, daemon=True)

    def __enter__(self):
        self.thread.start()
        return self

    def __exit__(self, *args):
        self.stop.set()
        self.thread.join()

    def _print(self):
        elapsed = int(time.monotonic() - self.started)
        print(f"{self.message} | 本阶段已用时 {elapsed} 秒", flush=True)
        self.last_output = time.monotonic()

    def update(self, message: str, *, force: bool = False):
        with self.lock:
            self.message = message.replace("\r", " ").replace("\n", " ")
            if force or time.monotonic() - self.last_output >= self.interval:
                self._print()

    def write(self, line: str):
        with self.lock:
            print(line, end="", flush=True)
            self.last_output = time.monotonic()

    def _heartbeat(self):
        while not self.stop.wait(min(self.interval, 1.0)):
            with self.lock:
                if self.message and time.monotonic() - self.last_output >= self.interval:
                    self._print()


@dataclass(frozen=True)
class WorkflowConfig:
    collector_project: Path
    classifier_project: Path
    download_root: Path
    source_library: Path
    classified_library: Path
    collector_database: Path
    login_state: Path
    report_root: Path
    collector_python: str = sys.executable
    classifier_python: str = sys.executable
    limit: int = 30
    max_new: int = 200

    @classmethod
    def load(cls, path: Path):
        raw = yaml.safe_load(path.read_text(encoding="utf-8-sig"))
        if not isinstance(raw, dict):
            raise ValueError("工作流配置必须是YAML对象")
        base = path.resolve().parent
        paths = {}
        for key in ("collector_project", "classifier_project", "download_root", "source_library",
                    "classified_library", "collector_database", "login_state", "report_root"):
            value = Path(raw[key])
            paths[key] = (value if value.is_absolute() else base / value).resolve()
        return cls(**paths, collector_python=raw.get("collector_python") or sys.executable,
                   classifier_python=raw.get("classifier_python") or sys.executable,
                   limit=int(raw.get("limit", 30)), max_new=int(raw.get("max_new", 200)))

    def validate(self):
        if not 0 < self.limit <= self.max_new:
            raise ValueError("必须满足 0 < limit <= max_new")
        if not (self.collector_project / "zhihu_scraper.py").is_file():
            raise ValueError("下载项目入口不存在")
        if not (self.classifier_project / "zhihu_classifier/__main__.py").is_file():
            raise ValueError("分类项目入口不存在")
        if not self.source_library.is_dir() or not self.classified_library.is_dir():
            raise ValueError("原始库和分类库必须是已经存在的目录")
        roots = [self.download_root.resolve(), self.source_library.resolve(), self.classified_library.resolve()]
        for i, left in enumerate(roots):
            for right in roots[i + 1:]:
                if inside(left, right) or inside(right, left):
                    raise ValueError("下载目录、原始库、分类库必须互不包含")
        for data_root in roots:
            if inside(self.report_root.resolve(), data_root):
                raise ValueError("运行报告不能保存在文章目录中")
        taxonomy = yaml.safe_load((self.classifier_project / "taxonomy.yaml").read_text(encoding="utf-8-sig"))
        configured = Path(taxonomy["paths"]["classified_library"]["path"]).resolve()
        if configured != self.classified_library.resolve():
            raise ValueError("工作流目标与分类项目taxonomy.yaml不一致")


def inside(path: Path, root: Path) -> bool:
    return path == root or root in path.parents


def digest(path: Path) -> str:
    result = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            result.update(chunk)
    return result.hexdigest()


@contextmanager
def readonly_database(path: Path):
    connection = sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True)
    connection.row_factory = sqlite3.Row
    try:
        yield connection
    finally:
        connection.close()


def collector_runs(config: WorkflowConfig) -> dict:
    if not config.collector_database.is_file():
        return {}
    with readonly_database(config.collector_database) as connection:
        if not connection.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='archive_runs'").fetchone():
            return {}
        rows = connection.execute("SELECT run_id,status,new_count FROM archive_runs WHERE source_method='playwright'").fetchall()
    return {row["run_id"]: dict(row) for row in rows}


def archived_items(config: WorkflowConfig) -> list:
    if not config.collector_database.is_file():
        return []
    with readonly_database(config.collector_database) as connection:
        if not connection.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='archive_items'").fetchone():
            return []
        rows = connection.execute("""SELECT content_key,markdown_path,image_dir FROM archive_items
            WHERE status='success' AND source_method='playwright' ORDER BY scraped_at,content_key""").fetchall()
    return [dict(row) for row in rows]


FRONTMATTER = re.compile(r"\A---\r?\n(.*?)\r?\n---\r?\n?", re.S)
IMAGES = re.compile(r'!\[[^\]]*\]\(([^)]+)\)|<img[^>]+src=["\']([^"\']+)["\']', re.I)


def identity(metadata: dict) -> str | None:
    path = urlparse(str(metadata.get("source_url", ""))).path.rstrip("/")
    for kind, pattern in (("answer", r"/answer/(\d+)$"), ("article", r"/p/(\d+)$"),
                          ("pin", r"/pin/(\d+)$")):
        match = re.search(pattern, path)
        if match:
            return f"{kind}:{match.group(1)}"
    answer_id = metadata.get("zhihu_answer_id")
    return f"answer:{answer_id}" if answer_id else None


def metadata_and_body(path: Path):
    text = path.read_text(encoding="utf-8-sig")
    match = FRONTMATTER.match(text)
    if not match:
        raise ValueError("缺少YAML元数据")
    metadata = yaml.safe_load(match.group(1))
    if not isinstance(metadata, dict):
        raise ValueError("YAML元数据必须是对象")
    return metadata, text[match.end():]


def validate_package(path: Path, root: Path, content_key: str, *, check_content: bool = True):
    if path.is_symlink() or not inside(path.resolve(strict=True), root):
        raise ValueError("Markdown超出下载目录或是符号链接")
    try:
        metadata, body = metadata_and_body(path)
    except (ValueError, yaml.YAMLError):
        if check_content:
            raise
        metadata, body = {}, path.read_text(encoding="utf-8-sig")
    if check_content:
        if not metadata.get("title") or identity(metadata) != content_key:
            raise ValueError("标题缺失或元数据内容ID与下载记录不一致")
        if "正文提取失败" in body:
            raise ValueError("正文提取失败占位内容，未进入归档和分类")
    attachment_root = path.with_suffix("")
    attachments = []
    if attachment_root.exists():
        if attachment_root.is_symlink() or not attachment_root.is_dir():
            raise ValueError("同名附件路径不是普通目录")
        for file in sorted(attachment_root.rglob("*")):
            if file.is_symlink() or not inside(file.resolve(), attachment_root.resolve()):
                raise ValueError("附件超出同名目录或是符号链接")
            if file.is_file():
                attachments.append(file)
    remote_images = 0
    for match in IMAGES.finditer(body):
        reference = (match.group(1) or match.group(2)).strip()
        if reference.startswith(("http://", "https://", "//")):
            remote_images += 1
            continue
        if reference.startswith("data:"):
            continue
        reference = unquote(reference.split("#", 1)[0].split("?", 1)[0])
        target = (path.parent / reference).resolve()
        if not inside(target, attachment_root.resolve()) or not target.is_file():
            raise ValueError("本地图片引用缺失或不在同名附件目录")
    return attachments, remote_images


def publish_file(source: Path, target: Path, expected_hash: str):
    target.parent.mkdir(parents=True, exist_ok=True)
    if target.exists():
        if not target.is_file() or digest(target) != expected_hash:
            raise FileExistsError(f"目标已存在且内容不同：{target}")
        return False
    descriptor, name = tempfile.mkstemp(prefix=".zhihu-sync-", suffix=".tmp", dir=target.parent)
    temporary = Path(name)
    try:
        with os.fdopen(descriptor, "wb") as writer, source.open("rb") as reader:
            shutil.copyfileobj(reader, writer)
        if digest(temporary) != expected_hash:
            raise OSError("同步临时文件哈希不一致")
        os.link(temporary, target)
    finally:
        # 仅删除本次创建的一个明确临时文件。
        temporary.unlink(missing_ok=True)
    return True


def sync_archives(config: WorkflowConfig, *, dry_run: bool = False, progress=None):
    """正文最后发布，保证分类扫描只看见完整文章包；旧原文绝不覆盖。"""
    download = config.download_root.resolve()
    source = config.source_library.resolve()
    index = {}
    index_errors = []
    notify = progress or (lambda message, **kwargs: None)
    notify("[原始备份] 正在扫描已有文章，建立去重索引……", force=True)
    scanned = 0
    for path in source.rglob("*.md"):
        notify(f"[原始备份] 已扫描 {scanned} 篇；正在读取：{path.name}")
        try:
            metadata, _ = metadata_and_body(path)
            key = identity(metadata)
            if key:
                index.setdefault(key, []).append(path)
        except (OSError, UnicodeError, ValueError, yaml.YAMLError) as exc:
            index_errors.append({"path": str(path), "error": str(exc)})
        scanned += 1
    notify(f"[原始备份] 扫描完成：{scanned} 篇；正在读取下载记录……", force=True)
    result = {"copied": 0, "would_copy": 0, "skipped": 0, "attachments_copied": 0,
              "errors": [], "warnings": [], "index_warnings": index_errors, "items": []}
    items = archived_items(config)
    total = len(items)
    notify(f"[原始备份] 共 {total} 篇下载文章待核对；校验正文、图片及已有备份……", force=True)
    for number, item in enumerate(items, 1):
        path = Path(item["markdown_path"])
        key = item["content_key"]
        notify(f"[原始备份] {number}/{total}；正在校验文章及附件：{path.name}")
        entry = {"content_key": key, "download_path": str(path)}
        try:
            if not path.is_absolute():
                raise ValueError("下载数据库中的文章路径必须是绝对路径")
            if path.is_symlink() or not inside(path.resolve(strict=True), download):
                raise ValueError("Markdown超出下载目录或是符号链接")
            relative = path.resolve().relative_to(download)
            target = (source / relative).resolve()
            if not inside(target, source):
                raise ValueError("目标超出原始库")
            hash_value = digest(path)
            already_archived = target.is_file() and not target.is_symlink() and digest(target) == hash_value
            attachments, remote_images = validate_package(path, download, key, check_content=not already_archived)
            if already_archived:
                try:
                    old_metadata, old_body = metadata_and_body(path)
                    old_content_ok = (bool(old_metadata.get("title")) and identity(old_metadata) == key
                                      and "正文提取失败" not in old_body)
                except (ValueError, yaml.YAMLError):
                    old_content_ok = False
                if not old_content_ok:
                    result["warnings"].append({"content_key": key,
                        "reason": "已有同字节原文存在历史元数据或正文异常，本轮跳过且不改写"})
            existing = index.get(key, [])
            if existing and target not in existing:
                if not any(digest(file) == hash_value for file in existing):
                    raise FileExistsError("同一知乎内容ID已有不同原文版本，保留旧文件并等待人工处理")
                target = next(file for file in existing if digest(file) == hash_value)
            if remote_images:
                result["warnings"].append({"content_key": key, "remote_images": remote_images,
                                           "reason": "仍含远程图片链接，离线阅读可能缺图"})
            entry["source_path"] = str(target)
            if target.exists():
                if target.is_symlink() or not target.is_file() or digest(target) != hash_value:
                    raise FileExistsError("同名原始文章不同，拒绝覆盖")
                # 旧原始包只读；缺失附件不擅自修补旧备份。
                for attachment_number, file in enumerate(attachments, 1):
                    notify(f"[原始备份] {number}/{total}；核对已有附件 {attachment_number}/{len(attachments)}：{path.name}")
                    copied_attachment = target.with_suffix("") / file.relative_to(path.with_suffix(""))
                    if not copied_attachment.is_file() or digest(copied_attachment) != digest(file):
                        raise FileExistsError("已有原始文章的附件缺失或不同，拒绝改写旧包")
                result["skipped"] += 1
                entry["status"] = "skipped"
            elif dry_run:
                result["would_copy"] += 1
                entry["status"] = "would_copy"
            else:
                # 附件先校验并追加，Markdown最后独占发布。失败可再次运行补齐。
                for attachment_number, file in enumerate(attachments, 1):
                    notify(f"[原始备份] {number}/{total}；追加附件 {attachment_number}/{len(attachments)}：{path.name}")
                    target_file = (target.with_suffix("") / file.relative_to(path.with_suffix(""))).resolve()
                    if not inside(target_file, source):
                        raise ValueError("附件目标超出原始库")
                    result["attachments_copied"] += int(publish_file(file, target_file, digest(file)))
                if digest(path) != hash_value:
                    raise ValueError("下载原文在同步期间变化，暂不发布")
                notify(f"[原始备份] {number}/{total}；正在追加正文：{path.name}")
                publish_file(path, target, hash_value)
                result["copied"] += 1
                entry["status"] = "copied"
                index.setdefault(key, []).append(target)
        except (OSError, UnicodeError, ValueError, yaml.YAMLError) as exc:
            entry.update(status="error", error=str(exc))
            result["errors"].append(entry)
        result["items"].append(entry)
        status = {"copied": "已追加", "skipped": "已有备份，跳过", "would_copy": "预览待追加", "error": "异常，未完成"}[entry["status"]]
        detail = f"；原因：{entry['error']}" if entry["status"] == "error" else ""
        notify(f"[原始备份] {number}/{total} {status}：{path.name}{detail}",
               force=entry["status"] in {"copied", "error"})
    notify(f"[原始备份] 核对完成：新增 {result['copied']} 篇，已有备份跳过 {result['skipped']} 篇，"
           f"新增附件 {result['attachments_copied']} 个，异常 {len(result['errors'])} 篇；"
           f"提示 {len(result['warnings'])} 项，历史索引提示 {len(index_errors)} 项（详情见运行报告）", force=True)
    return result


@contextmanager
def workflow_lock(path: Path):
    """操作系统锁随进程退出释放；不删除目录，不遗留需要手动解锁的状态。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a+b") as handle:
        handle.seek(0, os.SEEK_END)
        if handle.tell() == 0:
            handle.write(b"0")
            handle.flush()
        handle.seek(0)
        if os.name == "nt":
            import msvcrt
            try:
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            except OSError as exc:
                raise RuntimeError("已有统一工作流正在运行") from exc
        else:
            import fcntl
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        try:
            yield
        finally:
            handle.seek(0)
            if os.name == "nt":
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(handle, fcntl.LOCK_UN)


def run_command(command: list[str], cwd: Path, env: dict, log_path: Path):
    output = []
    phase = "下载" if "zhihu_scraper.py" in command else "分类并生成Obsidian副本"
    with log_path.open("x", encoding="utf-8") as log, ConsoleProgress() as progress:
        progress.update(f"[{phase}] 子程序正在运行，等待下一条进度输出……")
        process = subprocess.Popen(command, cwd=cwd, env=env, stdout=subprocess.PIPE,
                                   stderr=subprocess.STDOUT, text=True, encoding="utf-8", errors="replace")
        try:
            for line in process.stdout:
                progress.write(line)
                log.write(line)
                log.flush()
                output.append(line)
            return {"returncode": process.wait(), "stdout": "".join(output)}
        except BaseException:
            process.terminate()
            try:
                process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait()
            raise
        finally:
            process.stdout.close()


def final_json(output: str):
    decoder = json.JSONDecoder()
    for match in re.finditer(r"(?m)^\{", output):
        try:
            parsed, end = decoder.raw_decode(output[match.start():])
            if isinstance(parsed, dict) and not output[match.start() + end:].strip():
                return parsed
        except ValueError:
            continue
    return None


def run_workflow(config: WorkflowConfig, *, confirm: bool = False, dry_run: bool = False,
                 skip_download: bool = False, runner=run_command):
    config.validate()
    if dry_run:
        return {"status": "preview", "download": "would_run" if not skip_download else "skipped",
                "sync": sync_archives(config, dry_run=True), "classification": "would_run"}
    if not confirm:
        raise ValueError("运行会下载文章、追加原始备份并调用分类模型，请显式添加 --confirm-run")
    run_id = datetime.now().strftime("%Y%m%d_%H%M%S") + "_" + uuid.uuid4().hex[:8]
    summary = {"run_id": run_id, "download": {"status": "skipped"}, "sync": None,
               "classification": None, "status": "running"}
    config.report_root.mkdir(parents=True, exist_ok=True)
    report = config.report_root / f"{run_id}.json"
    env = {**os.environ, "PYTHONUTF8": "1", "PYTHONIOENCODING": "utf-8", "PYTHONUNBUFFERED": "1"}
    with workflow_lock(config.report_root / "workflow.lock"):
        try:
            if skip_download:
                print("\n[阶段 1/3] 依次下载：本次跳过", flush=True)
            if not skip_download:
                print("\n[阶段 1/3] 依次下载：启动Playwright采集", flush=True)
                before = collector_runs(config)
                if not config.login_state.is_file():
                    summary["download"] = {"status": "failed", "error": "登录态不存在，请先运行下载项目init_login.py"}
                else:
                    command = [config.collector_python, "-u", "-B", "zhihu_scraper.py", "--limit", str(config.limit),
                               "--continue-to-boundary", "--max-new", str(config.max_new),
                               "--output-dir", str(config.download_root), "--db-file", str(config.collector_database),
                               "--state-file", str(config.login_state)]
                    result = runner(command, config.collector_project, env, config.report_root / f"{run_id}-download.log")
                    after = collector_runs(config)
                    new_runs = [value for key, value in after.items() if key not in before]
                    complete = (result["returncode"] == 0 and len(new_runs) == 1 and new_runs[0]["status"] == "complete")
                    summary["download"] = {"status": "complete" if complete else "incomplete",
                                           "returncode": result["returncode"], "runs": new_runs}
            download_status = {"complete": "完成", "incomplete": "未完整结束", "failed": "失败", "skipped": "跳过"}[summary["download"]["status"]]
            print(f"[阶段 1/3] 下载状态：{download_status}；接下来核对已保存文章", flush=True)
            print("\n[阶段 2/3] 追加原始备份：扫描、去重、校验并追加新文章和附件", flush=True)
            with ConsoleProgress() as progress:
                summary["sync"] = sync_archives(config, progress=progress.update)
            print("\n[阶段 3/3] 分类并生成Obsidian副本：启动整库扫描与去重，随后只分析待处理文章", flush=True)
            # 分类模块维持原入口与数据库；通过已有SOURCE_LIBRARY覆盖机制指定实际备份库。
            manifest = config.report_root / f"{run_id}-classification-manifest.json"
            class_env = {**env, "SOURCE_LIBRARY": str(config.source_library)}
            command = [config.classifier_python, "-u", "-B", "-m", "zhihu_classifier", "update", "--confirm-copy",
                       "--manifest", str(manifest)]
            result = runner(command, config.classifier_project, class_env,
                            config.report_root / f"{run_id}-classification.log")
            details = final_json(result["stdout"])
            summary["classification"] = {"returncode": result["returncode"], "summary": details,
                                         "manifest": str(manifest)}
            failed = (summary["download"]["status"] not in {"complete", "skipped"}
                      or summary["sync"]["errors"]
                      or result["returncode"] != 0 or details is None)
            summary["status"] = "partial" if failed else "completed"
            print(f"[阶段 3/3] 分类子程序已结束，退出码 {result['returncode']}", flush=True)
        except KeyboardInterrupt:
            summary.update(status="interrupted", error="用户中断，已停止子进程；下次可补处理")
        except Exception as exc:
            summary.update(status="failed", error=str(exc))
        finally:
            summary["report"] = str(report)
            with report.open("x", encoding="utf-8") as handle:
                json.dump(summary, handle, ensure_ascii=False, indent=2)
    return summary
