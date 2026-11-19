import sqlite3
from datetime import datetime, timedelta, timezone

from flask import (
    Blueprint,
    abort,
    flash,
    jsonify,
    redirect,
    render_template,
    request,
    url_for,
)

from . import logic
from .db import get_db

bp = Blueprint("main", __name__)

KIND_NAMES = {"readings": "读数", "swaps": "换表", "ledger": "台账"}


def data_version(conn):
    """数据版本号 = 最近一个生效批次 id。同一事务内读取，保证版本与内容一致。"""
    return conn.execute(
        "SELECT COALESCE(MAX(id), 0) AS v FROM uploads WHERE status = 'applied'"
    ).fetchone()["v"]


def list_points(conn):
    return [
        r["point"]
        for r in conn.execute(
            "SELECT point FROM ("
            "  SELECT point FROM readings UNION SELECT point FROM swaps"
            ") ORDER BY point"
        )
    ]


def record_rejected(conn, kind, filename, message, rows_received):
    """被拒批次单独落一条审计记录（不影响既有数据）；审计失败不掩盖原始错误。"""
    try:
        conn.execute("BEGIN IMMEDIATE")
        conn.execute(
            "INSERT INTO uploads(kind, filename, status, message, rows_received,"
            " rows_applied, created_at) VALUES (?,?,?,?,?,0,?)",
            (kind, filename, "rejected", message, rows_received, logic.now_ts()),
        )
        conn.execute("COMMIT")
    except sqlite3.Error:
        conn.execute("ROLLBACK")


@bp.route("/")
def index():
    conn = get_db()
    conn.execute("BEGIN")  # 单个读事务：页面看到的是同一版快照
    try:
        points = list_points(conn)
        uploads = conn.execute(
            "SELECT * FROM uploads ORDER BY id DESC LIMIT 50"
        ).fetchall()
        version = data_version(conn)
    finally:
        conn.execute("COMMIT")
    return render_template(
        "index.html",
        points=points,
        uploads=uploads,
        version=version,
        kind_names=KIND_NAMES,
    )


@bp.route("/upload/<kind>", methods=["POST"])
def upload(kind):
    if kind not in KIND_NAMES:
        abort(404)
    # 台账在线损核查页整份上传，失败后回到核查页；其余回首页
    target = "main.loss" if kind == "ledger" else "main.index"
    f = request.files.get("file")
    if f is None or not f.filename:
        flash("请选择要上传的 CSV 文件", "error")
        return redirect(url_for(target))
    try:
        text = f.read().decode("utf-8-sig")
    except UnicodeDecodeError:
        flash("上传被拒绝：文件编码须为 UTF-8", "error")
        return redirect(url_for(target))

    conn = get_db()
    # 1) 解析与格式校验（不进事务）
    try:
        if kind == "readings":
            parsed, raw_count = logic.parse_readings_csv(text)
        elif kind == "swaps":
            parsed, raw_count = logic.parse_swaps_csv(text)
        else:
            parsed, raw_count = logic.parse_ledger_csv(text)
    except logic.CsvError as e:
        record_rejected(conn, kind, f.filename, str(e), 0)
        flash(f"上传被拒绝：{e}", "error")
        return redirect(url_for(target))

    # 2) 单个写事务：原始记录与受影响结果一起生效；失败整体回滚，保持旧数据
    conn.execute("BEGIN IMMEDIATE")
    try:
        cur = conn.execute(
            "INSERT INTO uploads(kind, filename, status, message, rows_received,"
            " rows_applied, created_at) VALUES (?,?,?,?,?,0,?)",
            (kind, f.filename, "applied", "", raw_count, logic.now_ts()),
        )
        upload_id = cur.lastrowid
        if kind == "readings":
            applied, skipped = logic.apply_readings(conn, upload_id, parsed)
            msg = f"接收 {raw_count} 行，新增 {applied} 条，重复跳过 {skipped} 条"
        elif kind == "swaps":
            applied, skipped = logic.apply_swaps(conn, upload_id, parsed)
            msg = f"接收 {raw_count} 行，新增 {applied} 条，重复跳过 {skipped} 条"
        else:
            applied = logic.apply_ledger(conn, upload_id, parsed)
            msg = f"接收 {raw_count} 行，台账已整份替换为 {applied} 条接线时段"
        conn.execute(
            "UPDATE uploads SET rows_applied = ?, message = ? WHERE id = ?",
            (applied, msg, upload_id),
        )
        conn.execute("COMMIT")
        flash(f"{KIND_NAMES[kind]}上传成功：{msg}", "success")
    except logic.BatchRejected as e:
        conn.execute("ROLLBACK")
        record_rejected(conn, kind, f.filename, str(e), raw_count)
        flash(f"上传被拒绝：{e}", "error")
    except sqlite3.OperationalError as e:
        conn.execute("ROLLBACK")
        flash(f"系统繁忙，请重试（{e}）", "error")
    return redirect(url_for(target))


def _resolve_range(conn, point, start, end):
    """校验/补全日期范围，返回 (start_day, end_day)。缺省时取该点数据最近 31 天。"""
    if start and end:
        d0, d1 = logic.parse_day(start), logic.parse_day(end)
    else:
        row = conn.execute(
            "SELECT MIN(substr(ts,1,10)) AS lo, MAX(substr(ts,1,10)) AS hi"
            " FROM readings WHERE point = ?",
            (point,),
        ).fetchone()
        if row is None or row["lo"] is None:
            raise ValueError("该计量点暂无读数，请指定日期范围")
        d1 = logic.parse_day(end) if end else logic.parse_day(row["hi"])
        d0 = logic.parse_day(start) if start else logic.parse_day(row["lo"])
        if (d1 - d0).days > 30:
            d0 = d1 - timedelta(days=30)
    if d1 < d0:
        raise ValueError("结束日期早于开始日期")
    if (d1 - d0).days > logic.MAX_RANGE_DAYS:
        raise ValueError(f"日期范围过大（最多 {logic.MAX_RANGE_DAYS} 天）")
    return d0.strftime("%Y-%m-%d"), d1.strftime("%Y-%m-%d")


@bp.route("/usage")
def usage():
    point = request.args.get("point", "").strip()
    start = request.args.get("start", "").strip()
    end = request.args.get("end", "").strip()
    conn = get_db()
    conn.execute("BEGIN")  # 单个读事务：同一版完整结果
    try:
        points = list_points(conn)
        version = data_version(conn)
        days = gaps = total = None
        error = None
        if point:
            try:
                start, end = _resolve_range(conn, point, start, end)
                days, gaps, total = logic.usage_range(conn, point, start, end)
            except ValueError as e:
                error = str(e)
    finally:
        conn.execute("COMMIT")
    return render_template(
        "usage.html",
        points=points,
        point=point,
        start=start,
        end=end,
        days=days,
        gaps=gaps,
        total=total,
        error=error,
        version=version,
    )


@bp.route("/api/usage")
def api_usage():
    point = request.args.get("point", "").strip()
    start = request.args.get("start", "").strip()
    end = request.args.get("end", "").strip()
    if not (point and start and end):
        return jsonify(error="需要 point、start、end 参数（日期为 YYYY-MM-DD）"), 400
    conn = get_db()
    conn.execute("BEGIN")
    try:
        try:
            start, end = _resolve_range(conn, point, start, end)
        except ValueError as e:
            return jsonify(error=str(e)), 400
        days, gaps, total = logic.usage_range(conn, point, start, end)
        version = data_version(conn)
    finally:
        conn.execute("COMMIT")
    return jsonify(
        point=point,
        start=start,
        end=end,
        version=version,
        total_kwh=logic.fmt_num(total),
        days=[
            {"day": d["day"], "kwh": logic.fmt_num(d["kwh"]), "gap": d["gap"]}
            for d in days
        ],
        gaps=[{"start": g["start"], "end": g["end"]} for g in gaps],
    )


# ---------------------------------------------------------------- 线损核查

def _loss_query(conn, master, day):
    """在当前读事务内执行核查，返回 (result, error)。day 缺省取今天（UTC）。"""
    if not master:
        return None, None
    if not day:
        day = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    try:
        day = logic.parse_day(day).strftime("%Y-%m-%d")
    except ValueError as e:
        return None, str(e)
    return logic.verify_day(conn, master, day), None


@bp.route("/loss")
def loss():
    master = request.args.get("master", "").strip()
    day = request.args.get("day", "").strip()
    conn = get_db()
    conn.execute("BEGIN")  # 单个读事务：台账、读数、换表记录来自同一快照
    try:
        masters = [
            r["master"]
            for r in conn.execute(
                "SELECT DISTINCT master FROM ledger ORDER BY master"
            )
        ]
        ledger = conn.execute(
            "SELECT master, sub, start_ts, end_ts FROM ledger"
            " ORDER BY master, sub, start_ts"
        ).fetchall()
        version = data_version(conn)
        result, error = _loss_query(conn, master, day)
        if result is not None:
            day = result["day"]
    finally:
        conn.execute("COMMIT")
    return render_template(
        "loss.html",
        masters=masters,
        ledger=ledger,
        master=master,
        day=day,
        result=result,
        error=error,
        version=version,
    )


@bp.route("/api/loss")
def api_loss():
    master = request.args.get("master", "").strip()
    day = request.args.get("day", "").strip()
    if not master:
        return jsonify(error="需要 master 参数（总表）"), 400
    conn = get_db()
    conn.execute("BEGIN")  # 单个读事务：同一快照
    try:
        result, error = _loss_query(conn, master, day)
        if error:
            return jsonify(error=error), 400
        version = data_version(conn)
    finally:
        conn.execute("COMMIT")
    segments = []
    for seg in result["segments"]:
        item = {
            "start": seg["start"],
            "end": seg["end"],
            "subs": seg["subs"],
            "status": seg["status"],
        }
        if seg["status"] == "ok":
            item.update(
                master_delta=logic.fmt_num(seg["master_delta"]),
                sub_deltas={s: logic.fmt_num(d) for s, d in seg["sub_deltas"]},
                diff=logic.fmt_num(seg["diff"]),
            )
        elif seg["status"] == "blocked":
            item.update(missing=seg["missing"], swaps=seg["swaps"])
        segments.append(item)
    return jsonify(
        master=result["master"],
        day=result["day"],
        version=version,
        segments=segments,
        total_diff=(
            logic.fmt_num(result["total"]) if result["total"] is not None else None
        ),
    )
