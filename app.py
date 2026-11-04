"""紅檜賓果 v3.6 穩定版 — 全場同步標記 / 功能卡 / 戰況儀表板 / 留言牆 / 結束投票 / 爭議投票
本機啟動：雙擊 start.bat，或執行  python -m streamlit run app.py

穩定版調整：
  1. 自動更新 6 秒；只更新「正在看的頁面」
  2. 所有玩家共用同一份快取快照；有人寫入時立即清除快取
  3. 寫入一律 BEGIN IMMEDIATE，資料庫忙碌時不卡死
  4. v3.6：不使用 st.metric / st.toast（這兩個元件需要瀏覽器另外下載程式檔，
     瀏覽器快取舊版時會出現「Failed to fetch dynamically imported module」紅框），改用自訂 HTML
"""
import functools
import json
import random
import sqlite3
import uuid
from contextlib import contextmanager
from datetime import datetime
from html import escape
from pathlib import Path
from zoneinfo import ZoneInfo

import pandas as pd
import streamlit as st

# ═════════════ 設定 ═════════════
GRID = 4
CELLS = GRID * GRID
HAND_LIMIT = 3                                      # 功能卡持有上限
REFRESH_SEC = 6                                     # 自動更新秒數
SNAP_TTL = 3                                        # 共用快照快取秒數
DISPUTE_SEC = 180                                   # 爭議投票時限（秒）
DB_TIMEOUT = 4                                      # 資料庫忙碌時最多等幾秒
TZ = ZoneInfo("Asia/Taipei")
DB_PATH = Path.home() / "red_bingo" / "bingo_v3.db"
QFILE = Path(__file__).parent / "questions.txt"
try:
    ADMIN_PASSWORD = st.secrets["ADMIN_PASSWORD"]
except Exception:
    ADMIN_PASSWORD = "1234"   # 本機測試用；部署時請在 Streamlit Secrets 設定
# ═══════════════════════════════

LINES = ([[r * GRID + c for c in range(GRID)] for r in range(GRID)]
         + [[r * GRID + c for r in range(GRID)] for c in range(GRID)]
         + [[i * GRID + i for i in range(GRID)], [i * GRID + GRID - 1 - i for i in range(GRID)]])

# 功能卡：代碼 → (圖示, 名稱, 使用對象, 說明)
CARDS = {
    "shuffle":    ("🔀", "洗牌",     "指定一人",     "把對方 16 格的位置全部重新洗牌（已中的格子也會移動）。可能拆掉他的連線，也可能直接送他賓果！"),
    "swap":       ("🔁", "換題",     "指定一人",     "選對方一格「還沒中」的題目，換成題庫隨機一題（運氣不好可能換到已發生的題目）。"),
    "assign":     ("🎯", "指定換題", "指定一人",     "選對方一格「還沒中」的題目，再從題庫指定一題換進去。"),
    "steal":      ("❌", "拔答案",   "指定一人",     "選對方一格「已經中」的題目，換成一題「還沒發生」的題目，讓他少中一格。對已賓果的人使用可以拆掉他的連線！"),
    "extinguish": ("🧯", "滅火",     "指定聽牌的人", "只能對正在冒火（差一格賓果）的人使用：把他聽牌線上一格已中的題目換成未發生的題目。"),
    "shield":     ("🛡️", "護盾",     "被動・自動",   "不用按使用，放在手上就有效：擋下下一張別人對你使用的卡（含大地震），擋下後這張卡消失。"),
    "reflect":    ("🪞", "反彈",     "被動・自動",   "不用按使用，放在手上就有效：下一張別人對你使用的攻擊卡，改由出卡的人自己承受（效果隨機），反彈後這張卡消失。遇到大地震時等同護盾。"),
    "quake":      ("🌪️", "大地震",   "全部人",       "全場所有人（包含你自己、已賓果的人）卡片位置全部重新洗牌。"),
    "rescue":     ("🍀", "自救",     "自己",         "把自己一格「還沒中」的題目換成題庫隨機一題。"),
}
ATTACKS = {"shuffle", "swap", "assign", "steal", "extinguish"}
PASSIVE = {"shield", "reflect"}


def now() -> str:
    return datetime.now(TZ).strftime("%Y-%m-%d %H:%M:%S")


def secs_since(ts: str) -> float:
    t = datetime.strptime(ts, "%Y-%m-%d %H:%M:%S").replace(tzinfo=TZ)
    return (datetime.now(TZ) - t).total_seconds()


# ═════════════ 資料庫 ═════════════
@contextmanager
def db(write=False):
    """write=True：BEGIN IMMEDIATE 先搶寫入鎖，避免多人同時寫入互卡；結束後清除共用快照"""
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    c = sqlite3.connect(DB_PATH, timeout=DB_TIMEOUT, isolation_level=None)
    c.row_factory = sqlite3.Row
    try:
        if write:
            c.execute("BEGIN IMMEDIATE")
        yield c
        if write:
            c.execute("COMMIT")
    except Exception:
        if write and c.in_transaction:
            c.execute("ROLLBACK")
        raise
    finally:
        c.close()
    if write:
        load_snapshot.clear()


def safe_action(func):
    """按鈕 callback 包裝：資料庫忙碌時不讓畫面當掉，改顯示提示"""
    @functools.wraps(func)
    def wrapper(*args, **kwargs):
        try:
            return func(*args, **kwargs)
        except sqlite3.OperationalError:
            st.session_state.flash = ("warning", "⏳ 太多人同時操作，請再按一次")
    return wrapper


def file_questions():
    if not QFILE.exists():
        return []
    return [l.strip() for l in QFILE.read_text(encoding="utf-8-sig").splitlines() if l.strip()]


@st.cache_resource
def init_db():
    """整個伺服器只執行一次"""
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    c = sqlite3.connect(DB_PATH, timeout=30)
    c.execute("PRAGMA journal_mode=WAL")
    c.executescript("""
    CREATE TABLE IF NOT EXISTS questions(id INTEGER PRIMARY KEY AUTOINCREMENT, text TEXT UNIQUE NOT NULL,
        active INTEGER NOT NULL DEFAULT 1, updated_at TEXT);
    CREATE TABLE IF NOT EXISTS players(pid TEXT PRIMARY KEY, name TEXT UNIQUE NOT NULL COLLATE NOCASE,
        created_at TEXT, board TEXT NOT NULL, shield INTEGER DEFAULT 0, reflect INTEGER DEFAULT 0,
        listen_at TEXT, bingo_at TEXT, lines INTEGER DEFAULT 0);
    CREATE TABLE IF NOT EXISTS happened(text TEXT PRIMARY KEY, by_name TEXT, at TEXT);
    CREATE TABLE IF NOT EXISTS hand(id INTEGER PRIMARY KEY AUTOINCREMENT, pid TEXT, card TEXT, got_at TEXT);
    CREATE TABLE IF NOT EXISTS messages(id INTEGER PRIMARY KEY AUTOINCREMENT, at TEXT, kind TEXT,
        name TEXT, text TEXT);
    CREATE TABLE IF NOT EXISTS game(k TEXT PRIMARY KEY, v TEXT);
    CREATE TABLE IF NOT EXISTS votes(vote_id TEXT, pid TEXT, yes INTEGER, at TEXT, PRIMARY KEY(vote_id, pid));
    CREATE TABLE IF NOT EXISTS disputes(id TEXT PRIMARY KEY, q TEXT, mode TEXT, claim TEXT, by_pid TEXT,
        by_name TEXT, at TEXT, status TEXT DEFAULT 'open');
    CREATE INDEX IF NOT EXISTS ix_hand_pid ON hand(pid);
    """)
    try:
        c.execute("ALTER TABLE players ADD COLUMN lines INTEGER DEFAULT 0")
    except sqlite3.OperationalError:
        pass
    c.executemany("INSERT OR IGNORE INTO questions(text, active, updated_at) VALUES(?,1,?)",
                  [(q, now()) for q in file_questions()])
    c.commit()
    c.close()
    return True


def gget(c, k, default=""):
    r = c.execute("SELECT v FROM game WHERE k=?", (k,)).fetchone()
    return r["v"] if r else default


def gset(c, k, v):
    c.execute("INSERT INTO game(k, v) VALUES(?,?) ON CONFLICT(k) DO UPDATE SET v=excluded.v", (k, v))


def is_ended(c):
    return gget(c, "status") == "ended"


def active_questions(c):
    return [r["text"] for r in c.execute("SELECT text FROM questions WHERE active=1 ORDER BY id")]


def happened_set(c):
    return {r["text"] for r in c.execute("SELECT text FROM happened")}


def get_player(c, pid):
    r = c.execute("SELECT * FROM players WHERE pid=?", (pid,)).fetchone()
    return dict(r, board=json.loads(r["board"])) if r else None


def all_players(c):
    return [dict(r, board=json.loads(r["board"])) for r in c.execute("SELECT * FROM players ORDER BY created_at")]


def player_count(c):
    return c.execute("SELECT COUNT(*) FROM players").fetchone()[0]


def set_board(c, pid, board):
    c.execute("UPDATE players SET board=? WHERE pid=?", (json.dumps(board, ensure_ascii=False), pid))


def post(c, text, kind="sys", name=""):
    c.execute("INSERT INTO messages(at, kind, name, text) VALUES(?,?,?,?)", (now(), kind, name, text))


def give_card(c, pid, card=None):
    if c.execute("SELECT COUNT(*) FROM hand WHERE pid=?", (pid,)).fetchone()[0] >= HAND_LIMIT:
        return None
    card = card or random.choice(list(CARDS))
    c.execute("INSERT INTO hand(pid, card, got_at) VALUES(?,?,?)", (pid, card, now()))
    return card


def consume_defense(c, pid, allow_reflect=True):
    """被攻擊時自動檢查手牌：先反彈、再護盾；有就消耗一張並回傳種類"""
    order = ["reflect", "shield"] if allow_reflect else ["shield", "reflect"]
    for kind in order:
        r = c.execute("SELECT id FROM hand WHERE pid=? AND card=? ORDER BY id LIMIT 1", (pid, kind)).fetchone()
        if r:
            c.execute("DELETE FROM hand WHERE id=?", (r["id"],))
            return kind
    return None


# ═════════════ 投票狀態（讀取用） ═════════════
def vote_state(c):
    vid = gget(c, "vote_id")
    if not vid:
        return None
    total = player_count(c)
    rows = {r["pid"]: r["yes"] for r in c.execute("SELECT pid, yes FROM votes WHERE vote_id=?", (vid,))}
    yes = sum(1 for v in rows.values() if v)
    return {"id": vid, "by": gget(c, "vote_by"), "total": total, "yes": yes,
            "no": len(rows) - yes, "need": (total + 1) // 2, "mine": rows}


def dispute_state(c):
    d = c.execute("SELECT * FROM disputes WHERE status='open' ORDER BY at LIMIT 1").fetchone()
    if not d:
        return None
    d = dict(d)
    total = player_count(c)
    rows = {r["pid"]: r["yes"] for r in c.execute("SELECT pid, yes FROM votes WHERE vote_id=?", ("D" + d["id"],))}
    yes = sum(1 for v in rows.values() if v)
    d.update(total=total, yes=yes, no=len(rows) - yes, need=total // 2 + 1, mine=rows,
             left=max(0, int(DISPUTE_SEC - secs_since(d["at"]))))
    return d


# ═════════════ 共用快照（所有玩家共用，一次讀完） ═════════════
@st.cache_data(ttl=SNAP_TTL, show_spinner=False)
def load_snapshot():
    with db() as c:
        players = all_players(c)
        hands = {}
        for r in c.execute("SELECT * FROM hand ORDER BY id"):
            hands.setdefault(r["pid"], []).append(dict(r))
        return {
            "players": players,
            "hap": sorted(happened_set(c)),
            "hands": hands,
            "msgs": [dict(r) for r in c.execute("SELECT * FROM messages ORDER BY id DESC LIMIT 40")],
            "bank": active_questions(c),
            "ended": is_ended(c),
            "ended_at": gget(c, "ended_at"),
            "vote": vote_state(c),
            "dispute": dispute_state(c),
        }


def snapshot():
    """讀取快照；資料庫忙碌時回傳 None（畫面維持上一輪，下一輪自動再試）"""
    try:
        s = load_snapshot()
    except sqlite3.OperationalError:
        return None
    d = s["dispute"]
    if d and d["left"] <= 0:                       # 爭議投票逾時 → 結算一次
        try:
            with db(write=True) as c:
                settle_dispute(c)
            s = load_snapshot()
        except sqlite3.OperationalError:
            pass
    s = dict(s)
    s["hap"] = set(s["hap"])
    s["pmap"] = {p["pid"]: p for p in s["players"]}
    return s


# ═════════════ 結束投票 ═════════════
def open_vote(c, trigger_name):
    if is_ended(c) or gget(c, "vote_id"):
        return
    gset(c, "vote_id", uuid.uuid4().hex[:8])
    gset(c, "vote_by", trigger_name)
    post(c, f"🗳️ {trigger_name} 賓果了，發起「是否結束此局」投票！半數同意即結束")


def check_vote(c):
    v = vote_state(c)
    if not v:
        return
    if v["yes"] >= v["need"]:
        gset(c, "status", "ended")
        gset(c, "ended_at", now())
        gset(c, "vote_id", "")
        post(c, f"🏁 投票通過（同意 {v['yes']} / {v['total']} 人），本局結束！")
    elif v["no"] > v["total"] - v["need"]:
        gset(c, "vote_id", "")
        post(c, f"▶️ 投票未通過（反對 {v['no']} / {v['total']} 人），遊戲繼續！下一次有人連線會再投票")


@safe_action
def cast_vote(pid, yes):
    with db(write=True) as c:
        v = vote_state(c)
        if not v or is_ended(c):
            return
        c.execute("INSERT OR REPLACE INTO votes VALUES(?,?,?,?)", (v["id"], pid, int(yes), now()))
        check_vote(c)


# ═════════════ 爭議投票 ═════════════
def settle_dispute(c):
    d = dispute_state(c)
    if not d:
        return
    verb = "成立" if d["mode"] == "add" else "撤銷"
    if d["yes"] >= d["need"]:
        c.execute("UPDATE disputes SET status='pass' WHERE id=?", (d["id"],))
        if d["mode"] == "add":
            c.execute("INSERT OR IGNORE INTO happened VALUES(?,?,?)", (d["q"], "投票認定", now()))
        else:
            c.execute("DELETE FROM happened WHERE text=?", (d["q"],))
        post(c, f"⚖️ 爭議投票通過（同意 {d['yes']} / {d['total']} 人）：「{d['q']}」{verb}！")
        evaluate_all(c)
    elif d["no"] > d["total"] - d["need"]:
        c.execute("UPDATE disputes SET status='fail' WHERE id=?", (d["id"],))
        post(c, f"⚖️ 爭議投票未通過（反對 {d['no']} / {d['total']} 人）：「{d['q']}」維持原狀")
    elif d["left"] <= 0:
        c.execute("UPDATE disputes SET status='fail' WHERE id=?", (d["id"],))
        post(c, f"⌛ 爭議投票時間到（同意 {d['yes']}，需要 {d['need']}）：「{d['q']}」維持原狀")


@safe_action
def start_dispute(pid, q, claim):
    claim = (claim or "").strip()[:40]
    if not claim:
        st.session_state.flash = ("warning", "請簡單描述發生了什麼（例如：有人提到台南小吃）")
        return
    with db(write=True) as c:
        me = get_player(c, pid)
        if not me or is_ended(c):
            return
        if dispute_state(c):
            st.session_state.flash = ("warning", "目前已經有一個爭議投票在進行，請等它結束")
            return
        mode = "remove" if q in happened_set(c) else "add"
        did = uuid.uuid4().hex[:8]
        c.execute("INSERT INTO disputes(id, q, mode, claim, by_pid, by_name, at) VALUES(?,?,?,?,?,?,?)",
                  (did, q, mode, claim, pid, me["name"], now()))
        c.execute("INSERT INTO votes VALUES(?,?,?,?)", ("D" + did, pid, 1, now()))  # 發起人自動同意
        ask = "算「" + q + "」嗎？" if mode == "add" else "「" + q + "」應該撤銷嗎？"
        post(c, f"⚖️ {me['name']} 發起爭議投票：「{claim}」{ask}")
        settle_dispute(c)


@safe_action
def cast_dispute(pid, yes):
    with db(write=True) as c:
        d = dispute_state(c)
        if not d:
            return
        c.execute("INSERT OR REPLACE INTO votes VALUES(?,?,?,?)", ("D" + d["id"], pid, int(yes), now()))
        settle_dispute(c)


@safe_action
def cancel_dispute(pid):
    with db(write=True) as c:
        d = dispute_state(c)
        if d and d["by_pid"] == pid:
            c.execute("UPDATE disputes SET status='cancel' WHERE id=?", (d["id"],))
            post(c, f"⚖️ {d['by_name']} 撤回了爭議投票「{d['q']}」")


# ═════════════ 遊戲邏輯 ═════════════
def analyze(board, hap):
    """回傳 (hits, 連線清單, 聽牌資訊[(線, 差的那格)])"""
    hits = [q in hap for q in board]
    lines = [ln for ln in LINES if all(hits[i] for i in ln)]
    near = [(ln, next(i for i in ln if not hits[i])) for ln in LINES if sum(hits[i] for i in ln) == GRID - 1]
    return hits, lines, near


def evaluate_all(c):
    """每次有變動後執行：更新連線數、首次賓果、拆線公告、發卡、發起結束投票"""
    hap = happened_set(c)
    players = all_players(c)
    info = {}
    for p in players:
        _, lines, near = analyze(p["board"], hap)
        info[p["pid"]] = (lines, near, p["lines"] or 0)
        if len(lines) != (p["lines"] or 0):
            c.execute("UPDATE players SET lines=? WHERE pid=?", (len(lines), p["pid"]))
        p["lines"] = len(lines)

    def deal_to_others(pid):
        return [o["name"] for o in players
                if o["pid"] != pid and not o["lines"] and give_card(c, o["pid"])]

    for p in players:
        lines, near, old = info[p["pid"]]
        n = len(lines)
        if n > old:
            got = deal_to_others(p["pid"])
            bonus = f"其他人各獲得一張功能卡：{'、'.join(got)}" if got else ""
            if not p["bingo_at"]:
                c.execute("UPDATE players SET bingo_at=? WHERE pid=?", (now(), p["pid"]))
                p["bingo_at"] = now()
                post(c, f"🏆 {p['name']} 賓果了！（首次賓果時間已記錄）" + bonus)
            else:
                post(c, f"🏆 {p['name']} 連線數 {old} → {n}！" + bonus)
            open_vote(c, p["name"])
        elif n < old:
            post(c, f"💥 {p['name']} 的連線被拆了！連線數 {old} → {n}")
        if near and not lines and not p["listen_at"]:
            c.execute("UPDATE players SET listen_at=? WHERE pid=?", (now(), p["pid"]))
            got = deal_to_others(p["pid"])
            post(c, f"🔥 {p['name']} 聽牌了（差一格）！" + (f"其他人各獲得一張功能卡：{'、'.join(got)}" if got else ""))


def random_new_question(c, board, need_unhappened=False):
    hap = happened_set(c) if need_unhappened else set()
    pool = [q for q in active_questions(c) if q not in board and q not in hap]
    return random.choice(pool) if pool else None


def apply_effect(c, card, victim, params=None):
    params = params or {}
    board = victim["board"]
    hits, _, near = analyze(board, happened_set(c))
    unhit = [i for i in range(CELLS) if not hits[i]]
    hit = [i for i in range(CELLS) if hits[i]]

    if card in ("shuffle", "quake"):
        random.shuffle(board)
        set_board(c, victim["pid"], board)
        return f"{victim['name']} 的卡片被洗牌了"

    if card in ("swap", "rescue", "assign"):
        idx = params.get("idx")
        if idx not in unhit:
            if not unhit:
                return "但沒有可替換的格子，無效"
            idx = random.choice(unhit)
        new_q = params.get("q") if card == "assign" else None
        if not new_q or new_q in board:
            new_q = random_new_question(c, board)
        if not new_q:
            return "但題庫沒有可替換的題目，無效"
        old = board[idx]
        board[idx] = new_q
        set_board(c, victim["pid"], board)
        return f"{victim['name']} 的「{old}」換成了「{new_q}」"

    if card == "steal":
        idx = params.get("idx")
        if idx not in hit:
            if not hit:
                return f"但 {victim['name']} 沒有已中的格子，無效"
            idx = random.choice(hit)
        new_q = random_new_question(c, board, need_unhappened=True)
        if not new_q:
            return "但沒有未發生的題目可換，無效"
        old = board[idx]
        board[idx] = new_q
        set_board(c, victim["pid"], board)
        return f"{victim['name']} 已中的「{old}」被換成了「{new_q}」"

    if card == "extinguish":
        if not near:
            return f"但 {victim['name']} 沒有在聽牌，無效"
        ln, _ = random.choice(near)
        idx = random.choice([i for i in ln if hits[i]])
        new_q = random_new_question(c, board, need_unhappened=True)
        if not new_q:
            return "但沒有未發生的題目可換，無效"
        old = board[idx]
        board[idx] = new_q
        set_board(c, victim["pid"], board)
        return f"{victim['name']} 的火被撲滅了（「{old}」→「{new_q}」）"
    return ""


@safe_action
def use_card(hand_id, user_pid, target_pid=None, params=None):
    with db(write=True) as c:
        if is_ended(c):
            st.session_state.flash = ("warning", "本局已結束，不能再使用功能卡")
            return
        row = c.execute("SELECT * FROM hand WHERE id=? AND pid=?", (hand_id, user_pid)).fetchone()
        me = get_player(c, user_pid)
        if not row or not me:
            st.session_state.flash = ("warning", "這張卡已經不在你手上了")
            return
        card = row["card"]
        if card in PASSIVE:
            st.session_state.flash = ("warning", "護盾 / 反彈是被動卡，放在手上就會自動生效")
            return
        icon, cname = CARDS[card][0], CARDS[card][1]
        c.execute("DELETE FROM hand WHERE id=?", (hand_id,))

        if card == "rescue":
            result = f"{icon} {me['name']} 使用自救：" + apply_effect(c, card, me, params)
        elif card == "quake":
            res = []
            for p in all_players(c):
                if p["pid"] != user_pid:
                    d = consume_defense(c, p["pid"], allow_reflect=False)
                    if d:
                        res.append(f"{p['name']}（{CARDS[d][0]}擋下）")
                        continue
                apply_effect(c, "shuffle", p)
                res.append(p["name"])
            result = f"{icon} {me['name']} 發動大地震！受影響：{'、'.join(res) or '無'}"
        else:
            tgt = get_player(c, target_pid)
            if not tgt:
                result = f"{icon} {me['name']} 的{cname}落空了（找不到對象）"
            else:
                d = consume_defense(c, tgt["pid"])
                if d == "reflect":
                    result = (f"🪞 {tgt['name']} 的反彈卡自動發動，{me['name']} 的{cname}被彈回去了！"
                              + apply_effect(c, card, me))
                elif d == "shield":
                    result = f"🛡️ {tgt['name']} 的護盾自動發動，擋下了 {me['name']} 的{cname}"
                else:
                    result = (f"{icon} {me['name']} 對 {tgt['name']} 使用{cname}："
                              + apply_effect(c, card, tgt, params))
        post(c, result)
        evaluate_all(c)
    st.session_state.flash = ("info", result)


@safe_action
def toggle_happened(q, by):
    with db(write=True) as c:
        if is_ended(c):
            st.session_state.flash = ("warning", "本局已結束，不能再標記")
            return
        if c.execute("SELECT 1 FROM happened WHERE text=?", (q,)).fetchone():
            c.execute("DELETE FROM happened WHERE text=?", (q,))
            post(c, f"↩️ {by} 取消了「{q}」")
        else:
            c.execute("INSERT INTO happened VALUES(?,?,?)", (q, by, now()))
            post(c, f"✅ {by} 標記「{q}」發生了")
        evaluate_all(c)


@safe_action
def join(name):
    name = (name or "").strip()
    if not name:
        return
    with db(write=True) as c:
        p = c.execute("SELECT pid FROM players WHERE name=?", (name,)).fetchone()
        if p:
            st.session_state.pid = p["pid"]
        else:
            if is_ended(c):
                st.session_state.flash = ("warning", "本局已結束，請按「重新開局」後再加入")
                return
            qs = active_questions(c)
            if len(qs) < CELLS:
                st.session_state.flash = ("error", f"題庫啟用題目不足 {CELLS} 題，請聯絡管理者")
                return
            pid = "RC-" + uuid.uuid4().hex[:6].upper()
            c.execute("INSERT INTO players(pid, name, created_at, board) VALUES(?,?,?,?)",
                      (pid, name, now(), json.dumps(random.sample(qs, CELLS), ensure_ascii=False)))
            give_card(c, pid)
            post(c, f"👋 {name} 加入了遊戲，獲得開局卡一張")
            evaluate_all(c)
            st.session_state.pid = pid
    st.query_params["p"] = st.session_state.pid


def logout():
    for k in ("pid", "seen_lines", "last_msg", "seen_end"):
        st.session_state.pop(k, None)
    st.query_params.clear()


@safe_action
def new_game(by_name=""):
    """重新開局：清空所有玩家、標記、手牌、留言、投票（題庫保留）"""
    with db(write=True) as c:
        for t in ("players", "happened", "hand", "messages", "votes", "game", "disputes"):
            c.execute(f"DELETE FROM {t}")
        post(c, f"🔄 {by_name or '有人'} 重新開局了！請重新輸入名字加入")
    logout()


@safe_action
def chat(name, text):
    text = (text or "").strip()[:50]
    if text:
        with db(write=True) as c:
            post(c, text, kind="chat", name=name)


def ranking_from(players, hap):
    rows = []
    for p in players:
        hits, lines, _ = analyze(p["board"], hap)
        rows.append({"p": p, "lines": len(lines), "hits": sum(hits)})
    rows.sort(key=lambda r: (-r["lines"], r["p"]["bingo_at"] or "~", -r["hits"]))
    return rows


# ═════════════ 樣式 / 動畫 ═════════════
CSS = """
<style>
.stApp {background: radial-gradient(circle at 10% 0%, #FFF3D6 0%, #FFFBF2 45%, #EEF6EF 100%);}
.hero {padding: 16px 24px; border-radius: 22px; margin-bottom: 8px;
  background: linear-gradient(120deg, #2E7D5B, #5BA77F); color: #fff;}
.hero h1 {margin: 0; font-size: 1.9rem; letter-spacing: 2px; color:#fff;}
.hero p {margin: 4px 0 0; opacity: .9;}
.card-title {background: #FFC000; color: #3a2a00; text-align: center; font-weight: 800;
  font-size: 1.2rem; padding: 8px; border-radius: 14px; margin-bottom: 8px;}

/* ── 頁面切換列 ── */
.st-key-viewbar button {border-radius: 99px; font-weight: 800; min-height: 46px;}
.st-key-viewbar button[kind="primary"] {background: #2E7D5B; border-color: #2E7D5B; color: #fff;}

/* ── 自訂統計卡（取代 st.metric） ── */
.stats {display: grid; grid-template-columns: repeat(auto-fit, minmax(130px, 1fr)); gap: 10px; margin-bottom: 14px;}
.stat {background: #fff; border-radius: 14px; padding: 10px 14px; box-shadow: 0 3px 10px rgba(46,125,91,.1);}
.stat .k {font-size: .82rem; color: #6b5d3d;} .stat .v {font-size: 1.7rem; font-weight: 900; color: #2B2B2B;}

/* ── 新戰報提示（取代 st.toast） ── */
.news {background: #2E7D5B; color: #fff; border-radius: 12px; padding: 8px 12px; margin-bottom: 8px;
  font-size: .9rem; animation: newsIn .4s ease both;}
@keyframes newsIn {from {opacity: 0; transform: translateY(-6px);} to {opacity: 1; transform: none;}}

/* ── 投票橫幅 ── */
.st-key-vote_box, .st-key-dispute_box {border-radius: 18px; padding: 14px 18px; margin-bottom: 10px;}
.st-key-vote_box {background: linear-gradient(120deg, #FFF1C2, #FFE08A); border: 3px solid #E0A100;}
.st-key-dispute_box {background: linear-gradient(120deg, #E8F0FF, #CFE0FF); border: 3px solid #5B7FD6;}
.vote-h {font-size: 1.2rem; font-weight: 900; color: #2B2B2B;}
.vote-bar {height: 12px; background: #fff; border-radius: 99px; overflow: hidden; margin: 8px 0 4px; display: flex;}
.vote-bar .y {background: #2E7D5B;} .vote-bar .n {background: #E8505B;}
.banner {border-radius: 12px; padding: 8px 12px; margin-bottom: 8px; font-size: .9rem;}
.banner.y {background: #FFF1C2;} .banner.b {background: #E8F0FF;}

/* ── 結算畫面 ── */
.final {background: linear-gradient(135deg, #2E7D5B, #5BA77F); color: #fff; border-radius: 22px;
  padding: 22px; text-align: center; margin-bottom: 14px;}
.final h2 {color: #fff; margin: 0; font-size: 2rem;}
.podium {display: flex; justify-content: center; align-items: flex-end; gap: 12px; margin-top: 16px;}
.podium div {background: #fff; color: #3a2a00; border-radius: 14px 14px 0 0; width: 140px; padding: 10px 6px;
  font-weight: 800; box-shadow: 0 6px 18px rgba(0,0,0,.15);}
.podium .p1 {height: 150px; background: #FFC000;} .podium .p2 {height: 115px; background: #E5E7EB;}
.podium .p3 {height: 90px; background: #F5C89A;}
.podium .medal {font-size: 2rem;}
.rank {width: 100%; border-collapse: collapse; background: #fff; border-radius: 12px; overflow: hidden; font-size: .9rem;}
.rank th {background: #F3EBD8; text-align: left; padding: 6px 10px;}
.rank td {border-top: 1px solid #eee; padding: 6px 10px;}
.rank tr.me td {background: #FFF1C2; font-weight: 800;}

/* ── 我的賓果盤（按鈕） ── */
div[class*="st-key-rbc_"] button {min-height: 84px; width: 100%; border-radius: 14px; padding: 6px;
  white-space: normal; word-break: break-word; line-height: 1.25; font-weight: 700;}
div[class*="st-key-rbc_"] button p {font-size: 1rem; white-space: normal; word-break: break-word;}
div[class*="st-key-rbc_"][class*="_L2"] button p {font-size: .88rem;}
div[class*="st-key-rbc_"][class*="_L3"] button p {font-size: .76rem;}
div[class*="st-key-rbc_"][class*="_L4"] button p {font-size: .66rem;}
div[class*="st-key-rbc_"][class*="_hit"] button {background: #2E7D5B; border-color: #2E7D5B; color: #fff;}
div[class*="st-key-rbc_"][class*="_line"] button {background: #FFC000; border-color: #E0A100; color: #3a2a00;}
div[class*="st-key-rbc_"][class*="_disp"] button {outline: 3px dashed #5B7FD6; outline-offset: -3px;}
div[class*="st-key-rbc_"][class*="_miss"] button {animation: missBlink .8s ease-in-out infinite alternate;
  border: 2px dashed #FF5A00;}
@keyframes missBlink {from {background: #FFF4E5;} to {background: #FFD0A8;}}

/* ── 冒火效果 ── */
.st-key-myboard_fire, .mini.fire {border-radius: 18px; animation: fireGlow .9s ease-in-out infinite alternate;}
.st-key-myboard_fire {padding: 10px;}
@keyframes fireGlow {
  from {box-shadow: 0 0 10px 3px rgba(255,90,0,.6), 0 0 26px 8px rgba(255,170,0,.35);}
  to   {box-shadow: 0 0 20px 7px rgba(255,50,0,.85), 0 0 48px 16px rgba(255,200,0,.5);}}
.flames {text-align: center; font-size: 1.6rem; line-height: 1; height: 32px; overflow: hidden;}
.flames span {display: inline-block; animation: flick .5s ease-in-out infinite alternate;}
@keyframes flick {from {transform: translateY(4px) scale(.85);} to {transform: translateY(-3px) scale(1.15);}}
.st-key-myboard_gold {border-radius: 18px; padding: 10px; box-shadow: 0 0 0 4px #FFC000, 0 0 26px 6px rgba(255,192,0,.5);}

/* ── 儀表板迷你盤 ── */
.minis {display: grid; grid-template-columns: repeat(auto-fill, minmax(250px, 1fr)); gap: 14px;}
.mini {background: #fff; border-radius: 16px; padding: 10px; box-shadow: 0 4px 14px rgba(46,125,91,.12);}
.mini.gold {box-shadow: 0 0 0 3px #FFC000, 0 4px 18px rgba(255,192,0,.4);}
.mini .hd {display: flex; justify-content: space-between; align-items: center; gap: 4px; flex-wrap: wrap;
  font-weight: 800; margin-bottom: 6px;}
.mini .tag {font-size: .75rem; padding: 2px 8px; border-radius: 99px; background: #EEF6EF; color: #2E7D5B;}
.mini .tag.f {background: #FFE3D1; color: #C2410C;} .mini .tag.g {background: #FFF1C2; color: #7A4B00;}
.mini .g4 {display: grid; grid-template-columns: repeat(4, 1fr); gap: 4px;}
.mini .c {min-height: 46px; border-radius: 8px; background: #FFFDF7; border: 1px solid #E9DFC6;
  font-size: .62rem; display: flex; align-items: center; justify-content: center; text-align: center;
  padding: 2px; word-break: break-word; line-height: 1.15; overflow: hidden;}
.mini .c.h {background: #2E7D5B; color: #fff; border-color: #2E7D5B;}
.mini .c.l {background: #FFC000; color: #3a2a00; border-color: #E0A100;}
.mini .c.m {border: 2px dashed #FF5A00;}
.mini .ft {font-size: .72rem; color: #6b5d3d; margin-top: 6px;}

/* ── 功能卡 ── */
.fcards {display: grid; grid-template-columns: repeat(auto-fill, minmax(190px, 1fr)); gap: 10px;}
.fcard {background: #fff; border-radius: 14px; padding: 10px 12px; border: 2px solid #E9DFC6;}
.fcard b {font-size: 1.05rem;} .fcard .who {font-size: .72rem; color: #fff; background: #2E7D5B;
  border-radius: 99px; padding: 1px 8px; margin-left: 6px;}
.fcard p {font-size: .82rem; margin: 6px 0 0; color: #444;}
.hand {display: flex; gap: 8px; flex-wrap: wrap; margin-bottom: 8px;}
.hand span {background: linear-gradient(135deg, #FFF3D6, #FFE08A); border: 2px solid #E0A100;
  border-radius: 12px; padding: 8px 10px; font-weight: 800;}
.hand span.pv {background: linear-gradient(135deg, #E3F3E8, #BFE3CB); border-color: #2E7D5B;}

/* ── 留言 ── */
.msgs {max-height: 560px; overflow-y: auto;}
.msg {padding: 6px 10px; border-radius: 12px; margin-bottom: 6px; font-size: .88rem; background: #fff;}
.msg.sys {background: #F3EBD8; color: #5a4a20; font-size: .8rem;}
.msg small {color: #999; margin-left: 6px;}
</style>
"""

WELCOME = """
<style>
#rb-welcome {position: fixed; inset: 0; z-index: 999999; display: flex; flex-direction: column;
  align-items: center; justify-content: center; pointer-events: none;
  background: linear-gradient(135deg, #2E7D5B, #5BA77F 55%, #FFC000); animation: rbOut .8s ease 2.6s forwards;}
#rb-welcome .tree {font-size: 96px; animation: rbGrow 1.2s cubic-bezier(.2,1.6,.4,1) both;}
#rb-welcome h2 {color: #fff; font-size: 2.4rem; margin: 8px 0 0; letter-spacing: 4px; animation: rbUp .8s ease .6s both;}
#rb-welcome p {color: #FFF7DA; margin: 6px 0 0; animation: rbUp .8s ease .9s both;}
@keyframes rbGrow {from {transform: scale(0) rotate(-20deg);} to {transform: scale(1) rotate(0);}}
@keyframes rbUp {from {opacity: 0; transform: translateY(20px);} to {opacity: 1; transform: none;}}
@keyframes rbOut {to {opacity: 0; visibility: hidden;}}
</style>
<div id="rb-welcome"><div class="tree">🌲</div><h2>紅檜賓果</h2><p>連線成功，準備開局！</p></div>
"""


def bingo_html(n_lines, word="BINGO!", sub=None):
    uid = uuid.uuid4().hex[:8]
    sub = sub or f"🌲 第 {n_lines} 條連線達成！"
    colors = ["#FFC000", "#2E7D5B", "#E8505B", "#5BA77F", "#FFFFFF", "#F59E0B"]
    confetti = "".join(
        f'<i style="left:{random.randint(0, 100)}%;background:{random.choice(colors)};'
        f'animation-delay:{random.random() * .8:.2f}s;animation-duration:{random.uniform(2.2, 3.4):.2f}s"></i>'
        for _ in range(60))
    letters = "".join(f'<span style="animation-delay:{k * .12:.2f}s">{escape(ch)}</span>' for k, ch in enumerate(word))
    return f"""
<style>
#bg-{uid} {{position: fixed; inset: 0; z-index: 999999; pointer-events: none; overflow: hidden;
  display: flex; flex-direction: column; align-items: center; justify-content: center;
  background: radial-gradient(circle, rgba(255,192,0,.55), rgba(46,125,91,.8)); animation: bgOut .7s ease 3.2s forwards;}}
#bg-{uid} .word span {{display: inline-block; font-size: clamp(48px, 12vw, 140px); font-weight: 900; color: #fff;
  text-shadow: 0 6px 0 #B97800, 0 10px 24px rgba(0,0,0,.35); animation: bgJump .7s cubic-bezier(.2,1.8,.4,1) both;}}
#bg-{uid} .sub {{color: #fff; font-size: 1.5rem; font-weight: 800; margin-top: 6px; animation: bgFade .6s ease .8s both;}}
#bg-{uid} i {{position: absolute; top: -20px; width: 10px; height: 16px; border-radius: 2px;
  animation-name: bgFall; animation-timing-function: linear; animation-fill-mode: both;}}
@keyframes bgJump {{from {{transform: translateY(-120px) scale(.3); opacity: 0;}} to {{transform: none; opacity: 1;}}}}
@keyframes bgFade {{from {{opacity: 0;}} to {{opacity: 1;}}}}
@keyframes bgFall {{from {{transform: rotate(0);}} to {{top: 105%; transform: rotate(720deg);}}}}
@keyframes bgOut {{to {{opacity: 0; visibility: hidden;}}}}
</style>
<div id="bg-{uid}">{confetti}<div class="word">{letters}</div><div class="sub">{escape(sub)}</div></div>
"""


FLAMES = '<div class="flames">' + "".join(
    f'<span style="animation-delay:{k * .07:.2f}s">🔥</span>' for k in range(14)) + "</div>"


def len_class(q):
    n = len(q)
    return "L1" if n <= 8 else "L2" if n <= 14 else "L3" if n <= 22 else "L4"


def md_escape(s):
    for ch in "\\`*_[]#<>|~$":
        s = s.replace(ch, "\\" + ch)
    return s


def stats_html(items):
    return '<div class="stats">' + "".join(
        f'<div class="stat"><div class="k">{escape(k)}</div><div class="v">{v}</div></div>' for k, v in items) + "</div>"


def mini_board_html(p, hap, hand_n):
    hits, lines, near = analyze(p["board"], hap)
    on_line = {i for ln in lines for i in ln}
    miss = {m for _, m in near}
    fire = bool(near) and not lines
    if lines:
        first = f"｜首次 {p['bingo_at'][11:]}" if p["bingo_at"] else ""
        tag, cls = f'<span class="tag g">🏆 賓果 {len(lines)} 線{first}</span>', "gold"
    elif fire:
        tag, cls = '<span class="tag f">🔥 聽牌中</span>', "fire"
    else:
        tag, cls = f'<span class="tag">中 {sum(hits)} 格</span>', ""
    if p["bingo_at"] and not lines:
        tag += f' <span class="tag g">曾賓果 {p["bingo_at"][11:]}</span>'
    cells = "".join(
        f'<div class="c {"l" if i in on_line else "h" if hits[i] else "m" if i in miss and fire else ""}">{escape(q)}</div>'
        for i, q in enumerate(p["board"]))
    return (f'<div class="mini {cls}"><div class="hd"><span>{escape(p["name"])}</span><span>{tag}</span></div>'
            f'<div class="g4">{cells}</div><div class="ft">{p["pid"]}｜🃏 {hand_n} 張</div></div>')


def msg_html(m):
    if m["kind"] == "sys":
        return f'<div class="msg sys">{escape(m["text"])}<small>{m["at"][11:]}</small></div>'
    return f'<div class="msg"><b>{escape(m["name"])}</b>：{escape(m["text"])}<small>{m["at"][11:]}</small></div>'


def final_html(rows):
    medals = ["🥇", "🥈", "🥉"]
    top = rows[:3]
    order = [1, 0, 2] if len(top) >= 2 else [0]
    pod = "".join(
        f'<div class="p{k + 1}"><div class="medal">{medals[k]}</div>{escape(top[k]["p"]["name"])}'
        f'<br><small>{top[k]["lines"]} 線｜中 {top[k]["hits"]} 格</small></div>'
        for k in order if k < len(top))
    return f'<div class="final"><h2>🏁 本局結束</h2><div>最終排名（目前連線數 → 首次賓果時間 → 中格數）</div><div class="podium">{pod}</div></div>'


def rank_table_html(rows, pid):
    trs = "".join(
        f'<tr class="{"me" if r["p"]["pid"] == pid else ""}"><td>{k + 1}</td><td>{escape(r["p"]["name"])}</td>'
        f'<td>{r["p"]["pid"]}</td><td>{r["lines"]}</td><td>{r["hits"]}</td><td>{r["p"]["bingo_at"] or ""}</td></tr>'
        for k, r in enumerate(rows))
    return ('<table class="rank"><tr><th>名次</th><th>姓名</th><th>卡號</th><th>目前連線</th><th>中格數</th>'
            f'<th>首次賓果時間</th></tr>{trs}</table>')


def bar_html(yes, no, total):
    t = max(total, 1)
    return (f'<div class="vote-bar"><div class="y" style="width:{yes / t * 100:.0f}%"></div>'
            f'<div class="n" style="width:{no / t * 100:.0f}%"></div></div>')


def show_flash():
    if "flash" in st.session_state:
        kind, text = st.session_state.pop("flash")
        getattr(st, kind)(text)


# ═════════════ 頁面 ═════════════
st.set_page_config(page_title="紅檜賓果", page_icon="🌲", layout="wide")
st.markdown(CSS, unsafe_allow_html=True)
init_db()

if not st.session_state.get("welcomed"):
    st.markdown(WELCOME, unsafe_allow_html=True)
    st.session_state.welcomed = True

S = snapshot()
if S is None:
    st.warning("⏳ 伺服器忙碌中，幾秒後請重新整理。")
    st.stop()

# 網址帶 ?p=卡號 → 重新整理自動回到原本的賓果局
if "pid" not in st.session_state and st.query_params.get("p") in S["pmap"]:
    st.session_state.pid = st.query_params["p"]

st.markdown('<div class="hero"><h1>🌲 紅檜賓果</h1><p>聽到誰講到卡上的題目就點下去，全場同步！差一格會冒火，善用功能卡干擾對手。</p></div>',
            unsafe_allow_html=True)

pid = st.session_state.get("pid")
me = S["pmap"].get(pid) if pid else None
if pid and not me:  # 已重新開局
    logout()
    pid = None


def restart_popover(key):
    """任何人都能按的重新開局（需二次確認）"""
    with st.popover("🔄 重新開局", use_container_width=True):
        st.markdown("**確定要重新開局嗎？**")
        st.caption("所有人的賓果盤、標記、功能卡、留言都會清空，大家需要重新輸入名字加入。題庫不受影響。")
        ok = st.checkbox("我確定，全場重新開始", key=f"rs_ok_{key}")
        st.button("確認重新開局", key=f"rs_btn_{key}", type="primary", disabled=not ok,
                  on_click=new_game, args=(me["name"] if me else "",), use_container_width=True)


if not me:
    a, b = st.columns([5, 1])
    with a:
        with st.form("join_form"):
            nm = st.text_input("請輸入您的名字", placeholder="輸入暱稱或姓名，大家認得出來就好", max_chars=20)
            st.caption("第一次加入會直接發一張亂數賓果卡；輸入已加入過的名字會回到原本的賓果局。")
            if st.form_submit_button("🎮 加入 / 回到我的賓果局", type="primary"):
                join(nm)
                st.rerun()
    with b:
        restart_popover("join")
    show_flash()
else:
    a, b, c3 = st.columns([4, 1, 1])
    a.markdown(f"👤 **{me['name']}**｜卡號 `{me['pid']}`｜💡 重新整理也會回到這局；換裝置時輸入同一個名字即可")
    b.button("切換玩家", on_click=logout, use_container_width=True)
    with c3:
        restart_popover("top")

with st.expander("📖 功能卡說明 & 規則（點我展開）"):
    st.markdown('<div class="fcards">' + "".join(
        f'<div class="fcard"><b>{i} {n}</b><span class="who">{w}</span><p>{escape(d)}</p></div>'
        for i, n, w, d in CARDS.values()) + "</div>", unsafe_allow_html=True)
    st.caption(f"拿卡：開局每人 1 張；有人「第一次」聽牌、或有人連成線（每多一條都算）時，其他目前沒有連線的人各得 1 張；"
               f"每人最多持有 {HAND_LIMIT} 張。已賓果的人仍可被攻擊、連線也能被拆；首次賓果時間會永久記錄。"
               "被攻擊時，手上的🪞反彈會先自動發動，沒有才輪到🛡️護盾。")
    st.caption("結束投票：有人連成線時自動發起「是否結束此局」投票，全體玩家半數（含）以上同意即結束。")
    st.caption(f"⚖️ 爭議投票：對某題「算不算」有疑問時，任何人都能發起。題目還沒發生 → 投「算不算發生」；"
               f"已經被標記 → 投「要不要撤銷」。全體玩家超過半數同意才成立，{DISPUTE_SEC // 60} 分鐘內沒過就維持原狀；"
               "同一時間只能有一個爭議投票。")
    st.caption("畫面出現紅色「Failed to fetch」錯誤時：電腦按 Ctrl + F5，手機關掉分頁重開即可。")

# 頁面切換：一般按鈕；只有正在看的頁面會執行與自動更新
VIEWS = ["🎯 我的賓果", "🔥 戰況儀表板 & 留言", "🔒 管理"]
st.session_state.setdefault("view", VIEWS[0])


def set_view(v):
    st.session_state.view = v


with st.container(key="viewbar"):
    for col, v in zip(st.columns(len(VIEWS)), VIEWS):
        col.button(v, key=f"nav_{VIEWS.index(v)}", on_click=set_view, args=(v,), use_container_width=True,
                   type="primary" if st.session_state.view == v else "secondary")
view = st.session_state.view


# ───────── 投票橫幅 / 結算 ─────────
def vote_banner(v, pid):
    with st.container(key="vote_box"):
        st.markdown(f'<div class="vote-h">🗳️ {escape(v["by"])} 賓果了！是否結束此局？</div>'
                    f'<div>同意 <b>{v["yes"]}</b>｜反對 <b>{v["no"]}</b>｜共 {v["total"]} 人，需要 <b>{v["need"]}</b> 票同意</div>'
                    + bar_html(v["yes"], v["no"], v["total"]), unsafe_allow_html=True)
        mine = v["mine"].get(pid)
        a, b, c2 = st.columns([1, 1, 2])
        a.button("✅ 同意結束", key="vote_yes", type="primary" if mine == 1 else "secondary",
                 on_click=cast_vote, args=(pid, True), use_container_width=True)
        b.button("▶️ 繼續玩", key="vote_no", type="primary" if mine == 0 else "secondary",
                 on_click=cast_vote, args=(pid, False), use_container_width=True)
        c2.caption("你已投：" + ("同意結束" if mine == 1 else "繼續玩" if mine == 0 else "尚未投票")
                   + "（投票結束前可以改票）")


def dispute_banner(d, pid):
    ask = f"算「{d['q']}」嗎？" if d["mode"] == "add" else f"「{d['q']}」應該撤銷嗎？"
    with st.container(key="dispute_box"):
        st.markdown(f'<div class="vote-h">⚖️ 爭議投票｜{escape(d["by_name"])}：「{escape(d["claim"])}」{escape(ask)}</div>'
                    f'<div>同意 <b>{d["yes"]}</b>｜反對 <b>{d["no"]}</b>｜共 {d["total"]} 人，需要 <b>{d["need"]}</b> 票'
                    f'（超過半數）｜約剩 {d["left"] // 60}:{d["left"] % 60:02d}</div>'
                    + bar_html(d["yes"], d["no"], d["total"]), unsafe_allow_html=True)
        mine = d["mine"].get(pid)
        a, b, c2 = st.columns([1, 1, 2])
        a.button("👍 算", key="dv_yes", type="primary" if mine == 1 else "secondary",
                 on_click=cast_dispute, args=(pid, True), use_container_width=True)
        b.button("👎 不算", key="dv_no", type="primary" if mine == 0 else "secondary",
                 on_click=cast_dispute, args=(pid, False), use_container_width=True)
        if d["by_pid"] == pid:
            c2.button("撤回這個投票", key="dv_cancel", on_click=cancel_dispute, args=(pid,))
        else:
            c2.caption("你已投：" + ("算" if mine == 1 else "不算" if mine == 0 else "尚未投票") + "（可改票）")


def final_screen(s, pid):
    rows = ranking_from(s["players"], s["hap"])
    if not st.session_state.get("seen_end"):
        st.markdown(bingo_html(0, word="GAME!", sub="🏁 本局結束，感謝參與！"), unsafe_allow_html=True)
        st.session_state.seen_end = True
    st.markdown(final_html(rows), unsafe_allow_html=True)
    rank = next((k for k, r in enumerate(rows) if r["p"]["pid"] == pid), None)
    if rank is not None:
        r = rows[rank]
        st.success(f"你的名次：第 {rank + 1} 名｜連線 {r['lines']} 條｜中 {r['hits']} 格")
    st.markdown(rank_table_html(rows, pid), unsafe_allow_html=True)
    st.caption(f"結束時間：{s['ended_at']}｜想再玩一局，按上方「🔄 重新開局」。")


# ───────── 我的賓果 ─────────
@st.fragment(run_every=REFRESH_SEC)
def play_area():
    s = snapshot()
    if s is None:
        st.caption("⏳ 伺服器忙碌，下一輪自動更新…")
        return
    pid = st.session_state.get("pid")
    if s["ended"]:
        final_screen(s, pid)
        return
    if not pid:
        st.info("先在上方輸入名字加入遊戲。")
        return
    me = s["pmap"].get(pid)
    if not me:
        st.warning("找不到你的賓果局（可能已重新開局），請重新整理後再加入。")
        return
    hap, bank, msgs = s["hap"], s["bank"], s["msgs"]
    others = [p for p in s["players"] if p["pid"] != pid]
    hand = s["hands"].get(pid, [])
    dsp = s["dispute"]
    st.session_state.seen_end = False

    if s["vote"]:
        vote_banner(s["vote"], pid)
    if dsp:
        dispute_banner(dsp, pid)
    show_flash()

    hits, lines, near = analyze(me["board"], hap)
    on_line = {i for ln in lines for i in ln}
    miss = {m for _, m in near}
    fire = bool(near) and not lines

    seen = st.session_state.setdefault("seen_lines", len(lines))
    if len(lines) > seen:
        st.markdown(bingo_html(len(lines)), unsafe_allow_html=True)
    st.session_state.seen_lines = len(lines)

    # 新戰報提示（自訂 HTML，取代 st.toast）
    last = st.session_state.setdefault("last_msg", msgs[0]["id"] if msgs else 0)
    news = [m for m in msgs if m["id"] > last and m["kind"] == "sys"][:3]
    if news:
        st.markdown("".join(f'<div class="news">📣 {escape(m["text"])}</div>' for m in reversed(news)),
                    unsafe_allow_html=True)
    if msgs:
        st.session_state.last_msg = max(last, msgs[0]["id"])

    left, right = st.columns([2.3, 1], gap="large")
    with left:
        status = (f"🏆 已賓果 {len(lines)} 線" if lines else "🔥 聽牌中！差一格" if fire
                  else f"已中 {sum(hits)} / {CELLS} 格")
        if me["bingo_at"] and not lines:
            status += f"｜曾於 {me['bingo_at'][11:]} 賓果，快補回來！"
        st.markdown(f'<div class="card-title">{escape(me["name"])} 的紅檜賓果　<small>{status}</small></div>',
                    unsafe_allow_html=True)
        box = "myboard_fire" if fire else "myboard_gold" if lines else "myboard"
        with st.container(key=box):
            if fire:
                st.markdown(FLAMES, unsafe_allow_html=True)
            for r in range(GRID):
                cols = st.columns(GRID)
                for col in range(GRID):
                    i = r * GRID + col
                    q = me["board"][i]
                    state = "_line" if i in on_line else "_hit" if hits[i] else "_miss" if (i in miss and fire) else ""
                    if dsp and dsp["q"] == q:
                        state += "_disp"
                    icon = "⭐ " if i in on_line else "✅ " if hits[i] else ""
                    cols[col].button(icon + md_escape(q), key=f"rbc_{i}_{len_class(q)}{state}", help=q,
                                     on_click=toggle_happened, args=(q, me["name"]), use_container_width=True)
        st.caption("點格子 = 標記這題「發生了」，所有卡上有這題的人會同步打勾；再點一次可取消。"
                   f"畫面每 {REFRESH_SEC} 秒自動更新。有爭議時用下方「⚖️ 發起爭議投票」。")

        with st.expander("⚖️ 發起爭議投票（這樣算不算？）"):
            if dsp:
                st.info("目前已有爭議投票進行中，請先在上方投票。")
            else:
                dq = st.selectbox("哪一題有爭議？", bank, key="dsp_q",
                                  format_func=lambda q: ("✅（已標記，投票撤銷）" if q in hap else "（未發生，投票認定）") + q)
                claim = st.text_input("發生了什麼？（40 字內）", key="dsp_claim", max_chars=40,
                                      placeholder="例如：有人提到台南小吃")
                st.caption("全體玩家超過半數同意才成立；發起人自動算一票同意。"
                           + ("此題已被標記，通過後會「撤銷」。" if dq in hap else "通過後這題會被標記為發生，所有人卡上同步打勾。"))
                st.button("發起投票", key="dsp_go", type="primary",
                          on_click=start_dispute, args=(pid, dq, claim))

        with st.expander("📢 標記不在我卡上的題目"):
            opts = [q for q in bank if q not in me["board"]]
            sel = st.selectbox("題目", opts, key="other_q", format_func=lambda q: ("✅ " if q in hap else "") + q)
            if sel:
                st.button("取消標記" if sel in hap else "標記發生", on_click=toggle_happened, args=(sel, me["name"]))

    with right:
        st.markdown(f"#### 🃏 我的功能卡（{len(hand)}/{HAND_LIMIT}）")
        if not hand:
            st.caption("目前沒有卡片。有人第一次聽牌或連成線時，你會拿到一張！")
        else:
            st.markdown('<div class="hand">' + "".join(
                f'<span class="{"pv" if h["card"] in PASSIVE else ""}">{CARDS[h["card"]][0]} {CARDS[h["card"]][1]}</span>'
                for h in hand) + "</div>", unsafe_allow_html=True)
            if any(h["card"] in PASSIVE for h in hand):
                st.caption("🟩 綠色為被動卡：放在手上就會在被攻擊時自動發動，不用按。")
            usable = [h for h in hand if h["card"] not in PASSIVE]
            if usable:
                hmap = {x["id"]: x for x in usable}
                h = hmap[st.selectbox("選擇要使用的卡", list(hmap), key="use_hand",
                                      format_func=lambda i: f"{CARDS[hmap[i]['card']][0]} {CARDS[hmap[i]['card']][1]}")]
                card = h["card"]
                st.caption(CARDS[card][3])
                target, params, ok = None, {}, True
                if card in ATTACKS:
                    pool = sorted(others, key=lambda p: (-(p["lines"] or 0), p["name"]))
                    if card == "extinguish":
                        pool = [p for p in pool if analyze(p["board"], hap)[2] and not analyze(p["board"], hap)[1]]
                    if not pool:
                        st.warning("目前沒有可以使用的對象")
                        ok = False
                    else:
                        pmap = {p["pid"]: p for p in pool}
                        t = pmap[st.selectbox("對象", list(pmap), key=f"tgt_{card}",
                                              format_func=lambda x: pmap[x]["name"]
                                              + (f"（🏆 {pmap[x]['lines']} 線）" if pmap[x]["lines"] else ""))]
                        target = t["pid"]
                        th = analyze(t["board"], hap)[0]
                        if card in ("swap", "assign", "steal"):
                            want_hit = card == "steal"
                            idxs = [i for i in range(CELLS) if th[i] == want_hit]
                            if not idxs:
                                st.warning("對方沒有符合條件的格子")
                                ok = False
                            else:
                                params["idx"] = st.selectbox("選擇對方的格子", idxs, key=f"idx_{card}",
                                                             format_func=lambda i: f"{i + 1:02d}｜{t['board'][i]}")
                        if card == "assign" and ok:
                            qopts = [q for q in bank if q not in t["board"]]
                            params["q"] = st.selectbox("換成這題", qopts, key="assign_q",
                                                       format_func=lambda q: ("（已發生）" if q in hap else "") + q)
                        st.caption("⚠️ 對方手上若有🪞反彈，效果會彈回你身上（隨機格子）；有🛡️護盾則會被擋下。")
                elif card == "rescue":
                    idxs = [i for i in range(CELLS) if not hits[i]]
                    if idxs:
                        params["idx"] = st.selectbox("選擇自己的格子", idxs, key="idx_rescue",
                                                     format_func=lambda i: f"{i + 1:02d}｜{me['board'][i]}")
                    else:
                        ok = False
                st.button(f"使用 {CARDS[card][0]} {CARDS[card][1]}", type="primary", disabled=not ok,
                          on_click=use_card, args=(h["id"], pid, target, params), use_container_width=True)

        st.markdown("#### 📰 最新戰報")
        st.markdown("".join(msg_html(m) for m in msgs[:6]) or "還沒有動態", unsafe_allow_html=True)


# ───────── 戰況儀表板 & 留言 ─────────
@st.fragment(run_every=REFRESH_SEC)
def dash_area():
    s = snapshot()
    if s is None:
        st.caption("⏳ 伺服器忙碌，下一輪自動更新…")
        return
    hap, ps = s["hap"], s["players"]
    v, d = s["vote"], s["dispute"]
    me_ = s["pmap"].get(st.session_state.get("pid"))
    show_flash()

    d1, d2 = st.columns([2.6, 1], gap="large")
    with d1:
        if s["ended"]:
            st.markdown('<div class="banner y">🏁 本局已結束，最終排名請看「我的賓果」頁面。</div>', unsafe_allow_html=True)
        elif v:
            st.markdown(f'<div class="banner y">🗳️ 結束投票進行中：同意 {v["yes"]}｜反對 {v["no"]}｜需要 {v["need"]} 票'
                        '（請到「我的賓果」投票）</div>', unsafe_allow_html=True)
        if d and not s["ended"]:
            st.markdown(f'<div class="banner b">⚖️ 爭議投票：「{escape(d["claim"])}」→「{escape(d["q"])}」｜'
                        f'同意 {d["yes"]}｜反對 {d["no"]}｜需要 {d["need"]} 票</div>', unsafe_allow_html=True)
        rows = []
        for p in ps:
            hits, lines, near = analyze(p["board"], hap)
            rows.append((0 if lines else 1 if near else 2, p["bingo_at"] or "~", -sum(hits), p))
        rows.sort(key=lambda x: x[:3])
        st.markdown(stats_html([
            ("參加人數", len(ps)),
            ("🏆 目前有連線", sum(1 for r in rows if r[0] == 0)),
            ("🔥 聽牌中", sum(1 for r in rows if r[0] == 1)),
            ("✅ 已發生題目", len(hap)),
        ]), unsafe_allow_html=True)
        if not ps:
            st.markdown("還沒有人加入。")
        else:
            st.markdown('<div class="minis">' + "".join(
                mini_board_html(p, hap, len(s["hands"].get(p["pid"], []))) for *_, p in rows) + "</div>",
                unsafe_allow_html=True)

    with d2:
        st.markdown("#### 💬 留言牆")
        if me_:
            with st.form("chat_form", clear_on_submit=True, border=False):
                t = st.text_input("留言", max_chars=50, label_visibility="collapsed", placeholder="說點什麼…（50 字內）")
                if st.form_submit_button("送出", use_container_width=True):
                    chat(me_["name"], t)
            ecols = st.columns(5)
            for e, col in zip(["🔥", "😂", "👀", "🙏", "🎉"], ecols):
                col.button(e, key=f"emo_{e}", on_click=chat, args=(me_["name"], e), use_container_width=True)
        else:
            st.caption("加入遊戲後就能留言")
        st.markdown('<div class="msgs">' + ("".join(msg_html(m) for m in s["msgs"]) or "還沒有留言") + "</div>",
                    unsafe_allow_html=True)


# ───────── 管理（不自動更新） ─────────
def admin_area():
    if not st.session_state.get("admin"):
        pw = st.text_input("管理者密碼", type="password")
        if st.button("登入"):
            if pw == ADMIN_PASSWORD:
                st.session_state.admin = True
                st.rerun()
            st.error("密碼錯誤")
        return
    if st.button("登出管理"):
        st.session_state.admin = False
        st.rerun()
    with db() as c:
        qdf = pd.DataFrame([dict(r) for r in c.execute("SELECT id, text, active, updated_at FROM questions ORDER BY id")])
        hdf = pd.DataFrame([dict(r) for r in c.execute("SELECT * FROM happened ORDER BY at DESC")],
                           columns=["text", "by_name", "at"])
    s = snapshot() or S
    players, v, d = s["players"], s["vote"], s["dispute"]

    st.subheader("🏁 本局狀態")
    if s["ended"]:
        st.success("本局已結束")
        if st.button("▶️ 恢復遊戲（取消結束，保留所有資料）"):
            with db(write=True) as c:
                gset(c, "status", "playing")
                post(c, "▶️ 管理者恢復了遊戲，繼續玩！")
            st.rerun()
    else:
        st.info("進行中" + (f"｜結束投票：同意 {v['yes']} / 反對 {v['no']} / 需要 {v['need']}" if v else ""))
        x, y, z = st.columns(3)
        if x.button("🏁 直接結束本局"):
            with db(write=True) as c:
                gset(c, "status", "ended")
                gset(c, "ended_at", now())
                gset(c, "vote_id", "")
                post(c, "🏁 管理者結束了本局！")
            st.rerun()
        if v and y.button("取消結束投票"):
            with db(write=True) as c:
                gset(c, "vote_id", "")
                post(c, "🗳️ 管理者取消了結束投票，遊戲繼續")
            st.rerun()
        if d and z.button("取消爭議投票"):
            with db(write=True) as c:
                c.execute("UPDATE disputes SET status='cancel' WHERE id=?", (d["id"],))
                post(c, f"⚖️ 管理者取消了爭議投票「{d['q']}」")
            st.rerun()
    st.caption("「🔄 重新開局」在首頁上方，所有玩家都能按。")

    st.subheader("📚 題庫")
    st.caption("想永久保留的題目請改 GitHub 上的 questions.txt。超過 22 字的題目會自動縮小字體，建議精簡。")
    with st.expander("➕ 新增題目"):
        txt = st.text_area("一行一題")
        if st.button("新增"):
            with db(write=True) as c:
                n = sum(c.execute("INSERT OR IGNORE INTO questions(text,active,updated_at) VALUES(?,1,?)",
                                  (t.strip(), now())).rowcount for t in txt.splitlines() if t.strip())
            st.success(f"已新增 {n} 題")
            st.rerun()
    e = qdf.rename(columns={"id": "編號", "text": "題目", "active": "啟用", "updated_at": "修改時間"})
    e["字數"] = e["題目"].str.len()
    e["啟用"] = e["啟用"] == 1
    ed = st.data_editor(e, hide_index=True, use_container_width=True, num_rows="fixed",
                        disabled=["編號", "修改時間", "字數"], key="q_editor")
    if st.button("💾 儲存題庫變更", type="primary"):
        n = 0
        with db(write=True) as c:
            for (_, o), (_, w) in zip(e.iterrows(), ed.iterrows()):
                t = str(w["題目"]).strip() or o["題目"]
                if t != o["題目"] or bool(w["啟用"]) != bool(o["啟用"]):
                    try:
                        c.execute("UPDATE questions SET text=?, active=?, updated_at=? WHERE id=?",
                                  (t, int(bool(w["啟用"])), now(), int(o["編號"])))
                        n += 1
                    except sqlite3.IntegrityError:
                        st.warning(f"「{t}」重複，未更新")
        st.success(f"已更新 {n} 題（已發給玩家的卡片不受影響）")
        st.rerun()

    st.subheader("✅ 已發生的題目")
    if hdf.empty:
        st.caption("還沒有")
    else:
        st.dataframe(hdf.rename(columns={"text": "題目", "by_name": "標記人", "at": "時間"}),
                     hide_index=True, use_container_width=True)
        rm = st.selectbox("撤回標記", hdf["text"].tolist())
        if st.button("撤回"):
            toggle_happened(rm, "管理者")
            st.rerun()

    st.subheader("🃏 玩家手牌（僅管理者可見，查證用）")
    if players:
        st.dataframe(pd.DataFrame([{
            "姓名": p["name"], "卡號": p["pid"],
            "手牌": "、".join(CARDS[h["card"]][1] for h in s["hands"].get(p["pid"], [])) or "（無）"}
            for p in players]), hide_index=True, use_container_width=True)

    st.subheader("🎁 發卡 / 留言")
    a, b = st.columns(2)
    if a.button("全員各發一張卡（目前無連線者）"):
        with db(write=True) as c:
            got = [p["name"] for p in all_players(c) if not p["lines"] and give_card(c, p["pid"])]
            post(c, f"🎁 管理者發卡給：{'、'.join(got) or '無（手牌已滿）'}")
        st.rerun()
    if b.button("清空留言"):
        with db(write=True) as c:
            c.execute("DELETE FROM messages WHERE kind='chat'")
        st.rerun()

    if players:
        rows = ranking_from(players, s["hap"])
        out = pd.DataFrame([{
            "名次": k + 1, "卡號": r["p"]["pid"], "姓名": r["p"]["name"], "加入時間": r["p"]["created_at"],
            "目前連線數": r["lines"], "中格數": r["hits"], "首次賓果時間": r["p"]["bingo_at"] or "",
            "卡片內容": "｜".join(r["p"]["board"])} for k, r in enumerate(rows)])
        st.download_button("⬇️ 下載戰況名單（CSV）", out.to_csv(index=False).encode("utf-8-sig"),
                           "bingo_result.csv", "text/csv")


if view == VIEWS[0]:
    play_area()
elif view == VIEWS[1]:
    dash_area()
else:
    admin_area()
