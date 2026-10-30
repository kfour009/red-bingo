"""紅檜賓果 v3.2 — 全場同步標記 / 功能卡 / 戰況儀表板 / 留言牆
本機啟動：雙擊 start.bat，或執行  python -m streamlit run app.py
需求：streamlit >= 1.40
"""
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
REFRESH_SEC = 3                                     # 自動更新秒數
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


# ═════════════ 資料庫 ═════════════
@contextmanager
def db():
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    c = sqlite3.connect(DB_PATH, timeout=15)
    c.row_factory = sqlite3.Row
    c.execute("PRAGMA journal_mode=WAL")
    try:
        yield c
        c.commit()
    finally:
        c.close()


def file_questions():
    if not QFILE.exists():
        return []
    return [l.strip() for l in QFILE.read_text(encoding="utf-8-sig").splitlines() if l.strip()]


def init_db():
    with db() as c:
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
        """)
        try:
            c.execute("ALTER TABLE players ADD COLUMN lines INTEGER DEFAULT 0")
        except sqlite3.OperationalError:
            pass
        c.executemany("INSERT OR IGNORE INTO questions(text, active, updated_at) VALUES(?,1,?)",
                      [(q, now()) for q in file_questions()])


def active_questions(c):
    return [r["text"] for r in c.execute("SELECT text FROM questions WHERE active=1 ORDER BY id")]


def happened_set(c):
    return {r["text"] for r in c.execute("SELECT text FROM happened")}


def get_player(c, pid):
    r = c.execute("SELECT * FROM players WHERE pid=?", (pid,)).fetchone()
    return dict(r, board=json.loads(r["board"])) if r else None


def all_players(c):
    return [dict(r, board=json.loads(r["board"])) for r in c.execute("SELECT * FROM players ORDER BY created_at")]


def set_board(c, pid, board):
    c.execute("UPDATE players SET board=? WHERE pid=?", (json.dumps(board, ensure_ascii=False), pid))


def post(c, text, kind="sys", name=""):
    c.execute("INSERT INTO messages(at, kind, name, text) VALUES(?,?,?,?)", (now(), kind, name, text))


def hand_of(c, pid):
    return [dict(r) for r in c.execute("SELECT * FROM hand WHERE pid=? ORDER BY id", (pid,))]


def give_card(c, pid, card=None):
    if len(hand_of(c, pid)) >= HAND_LIMIT:
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


# ═════════════ 遊戲邏輯 ═════════════
def analyze(board, hap):
    """回傳 (hits, 連線清單, 聽牌資訊[(線, 差的那格)])"""
    hits = [q in hap for q in board]
    lines = [ln for ln in LINES if all(hits[i] for i in ln)]
    near = [(ln, next(i for i in ln if not hits[i])) for ln in LINES if sum(hits[i] for i in ln) == GRID - 1]
    return hits, lines, near


def evaluate_all(c):
    """每次有變動後執行：更新連線數、記錄首次賓果、拆線公告、第一次聽牌發卡"""
    hap = happened_set(c)
    players = all_players(c)
    # 先算出所有人「目前」的連線數，再處理事件（避免用到舊資料）
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
            got = deal_to_others(p["pid"])   # 有人連成線 → 其他沒有連線的人各拿 1 張
            bonus = f"其他人各獲得一張功能卡：{'、'.join(got)}" if got else ""
            if not p["bingo_at"]:
                c.execute("UPDATE players SET bingo_at=? WHERE pid=?", (now(), p["pid"]))
                p["bingo_at"] = now()
                post(c, f"🏆 {p['name']} 賓果了！（首次賓果時間已記錄）" + bonus)
            else:
                post(c, f"🏆 {p['name']} 連線數 {old} → {n}！" + bonus)
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
    """對 victim 套用卡片效果，回傳效果描述；params=None 時隨機選擇"""
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


def use_card(hand_id, user_pid, target_pid=None, params=None):
    """使用功能卡（按鈕 callback）"""
    with db() as c:
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
        result = ""

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
        else:  # 攻擊卡
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


def toggle_happened(q, by):
    with db() as c:
        if c.execute("SELECT 1 FROM happened WHERE text=?", (q,)).fetchone():
            c.execute("DELETE FROM happened WHERE text=?", (q,))
            post(c, f"↩️ {by} 取消了「{q}」")
        else:
            c.execute("INSERT INTO happened VALUES(?,?,?)", (q, by, now()))
            post(c, f"✅ {by} 標記「{q}」發生了")
        evaluate_all(c)


def join(name):
    name = (name or "").strip()
    if not name:
        return
    with db() as c:
        p = c.execute("SELECT pid FROM players WHERE name=?", (name,)).fetchone()
        if p:
            st.session_state.pid = p["pid"]
        else:
            qs = active_questions(c)
            if len(qs) < CELLS:
                st.session_state.flash = ("error", f"題庫啟用題目不足 {CELLS} 題，請聯絡管理者")
                return
            pid = "RC-" + uuid.uuid4().hex[:6].upper()
            c.execute("INSERT INTO players(pid, name, created_at, board) VALUES(?,?,?,?)",
                      (pid, name, now(), json.dumps(random.sample(qs, CELLS), ensure_ascii=False)))
            card = give_card(c, pid)
            post(c, f"👋 {name} 加入了遊戲，獲得開局卡一張")
            evaluate_all(c)
            st.session_state.pid = pid
    st.query_params["p"] = st.session_state.pid


def logout():
    for k in ("pid", "seen_lines", "last_msg"):
        st.session_state.pop(k, None)
    st.query_params.clear()


def chat(name, text):
    text = (text or "").strip()[:50]
    if text:
        with db() as c:
            post(c, text, kind="chat", name=name)


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

/* ── 我的賓果盤（按鈕） ── */
div[class*="st-key-rbc_"] button {min-height: 84px; width: 100%; border-radius: 14px; padding: 6px;
  white-space: normal; word-break: break-word; line-height: 1.25; font-weight: 700;}
div[class*="st-key-rbc_"] button p {font-size: 1rem; white-space: normal; word-break: break-word;}
div[class*="st-key-rbc_"][class*="_L2"] button p {font-size: .88rem;}
div[class*="st-key-rbc_"][class*="_L3"] button p {font-size: .76rem;}
div[class*="st-key-rbc_"][class*="_L4"] button p {font-size: .66rem;}
div[class*="st-key-rbc_"][class*="_hit"] button {background: #2E7D5B; border-color: #2E7D5B; color: #fff;}
div[class*="st-key-rbc_"][class*="_line"] button {background: #FFC000; border-color: #E0A100; color: #3a2a00;}
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
.mini {background: #fff; border-radius: 16px; padding: 10px; margin-bottom: 14px;
  box-shadow: 0 4px 14px rgba(46,125,91,.12);}
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
.mini .c.m {border: 2px dashed #FF5A00; animation: missBlink .8s ease-in-out infinite alternate;}
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


def bingo_html(n_lines):
    uid = uuid.uuid4().hex[:8]
    colors = ["#FFC000", "#2E7D5B", "#E8505B", "#5BA77F", "#FFFFFF", "#F59E0B"]
    confetti = "".join(
        f'<i style="left:{random.randint(0, 100)}%;background:{random.choice(colors)};'
        f'animation-delay:{random.random() * .8:.2f}s;animation-duration:{random.uniform(2.2, 3.4):.2f}s"></i>'
        for _ in range(70))
    letters = "".join(f'<span style="animation-delay:{k * .12:.2f}s">{ch}</span>' for k, ch in enumerate("BINGO!"))
    return f"""
<style>
#bg-{uid} {{position: fixed; inset: 0; z-index: 999999; pointer-events: none; overflow: hidden;
  display: flex; flex-direction: column; align-items: center; justify-content: center;
  background: radial-gradient(circle, rgba(255,192,0,.55), rgba(46,125,91,.8)); animation: bgOut .7s ease 3.2s forwards;}}
#bg-{uid} .word span {{display: inline-block; font-size: clamp(56px, 13vw, 150px); font-weight: 900; color: #fff;
  text-shadow: 0 6px 0 #B97800, 0 10px 24px rgba(0,0,0,.35); animation: bgJump .7s cubic-bezier(.2,1.8,.4,1) both;}}
#bg-{uid} .sub {{color: #fff; font-size: 1.5rem; font-weight: 800; margin-top: 6px; animation: bgFade .6s ease .8s both;}}
#bg-{uid} i {{position: absolute; top: -20px; width: 10px; height: 16px; border-radius: 2px;
  animation-name: bgFall; animation-timing-function: linear; animation-fill-mode: both;}}
@keyframes bgJump {{from {{transform: translateY(-120px) scale(.3); opacity: 0;}} to {{transform: none; opacity: 1;}}}}
@keyframes bgFade {{from {{opacity: 0;}} to {{opacity: 1;}}}}
@keyframes bgFall {{from {{transform: rotate(0);}} to {{top: 105%; transform: rotate(720deg);}}}}
@keyframes bgOut {{to {{opacity: 0; visibility: hidden;}}}}
</style>
<div id="bg-{uid}">{confetti}<div class="word">{letters}</div><div class="sub">🌲 第 {n_lines} 條連線達成！</div></div>
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


# ═════════════ 頁面 ═════════════
st.set_page_config(page_title="紅檜賓果", page_icon="🌲", layout="wide")
st.markdown(CSS, unsafe_allow_html=True)
init_db()

if not st.session_state.get("welcomed"):
    st.markdown(WELCOME, unsafe_allow_html=True)
    st.session_state.welcomed = True

# 網址帶 ?p=卡號 → 重新整理自動回到原本的賓果局
if "pid" not in st.session_state and st.query_params.get("p"):
    with db() as c:
        if get_player(c, st.query_params["p"]):
            st.session_state.pid = st.query_params["p"]

st.markdown('<div class="hero"><h1>🌲 紅檜賓果</h1><p>聽到誰講到卡上的題目就點下去，全場同步！差一格會冒火，善用功能卡干擾對手。</p></div>',
            unsafe_allow_html=True)

pid = st.session_state.get("pid")
with db() as c:
    me = get_player(c, pid) if pid else None
if pid and not me:  # 遊戲被重置
    logout()
    pid = None

if not me:
    with st.form("join_form"):
        nm = st.text_input("請輸入您的名字", placeholder="例如：PEYTON", max_chars=20)
        st.caption("第一次加入會直接發一張亂數賓果卡；輸入已加入過的名字會回到原本的賓果局。")
        if st.form_submit_button("🎮 加入 / 回到我的賓果局", type="primary"):
            join(nm)
            st.rerun()
else:
    a, b = st.columns([4, 1])
    a.markdown(f"👤 **{me['name']}**｜卡號 `{me['pid']}`｜💡 重新整理也會回到這局；換裝置時輸入同一個名字即可")
    b.button("切換玩家", on_click=logout, use_container_width=True)

with st.expander("📖 功能卡說明（點我展開）"):
    st.markdown('<div class="fcards">' + "".join(
        f'<div class="fcard"><b>{i} {n}</b><span class="who">{w}</span><p>{escape(d)}</p></div>'
        for i, n, w, d in CARDS.values()) + "</div>", unsafe_allow_html=True)
    st.caption(f"規則：開局每人 1 張；有人「第一次」聽牌、或有人連成線（每多一條都算）時，其他目前沒有連線的人各得 1 張；每人最多持有 {HAND_LIMIT} 張。"
               "已賓果的人仍可被攻擊、連線也能被拆；首次賓果時間會永久記錄供兌獎。"
               "被攻擊時，手上的🪞反彈會先自動發動，沒有才輪到🛡️護盾。")

tab_play, tab_dash, tab_admin = st.tabs(["🎯 我的賓果", "🔥 戰況儀表板 & 留言", "🔒 管理"])


# ───────── 我的賓果 ─────────
@st.fragment(run_every=REFRESH_SEC)
def play_area():
    pid = st.session_state.get("pid")
    if not pid:
        st.info("先在上方輸入名字加入遊戲。")
        return
    with db() as c:
        me = get_player(c, pid)
        if not me:
            st.warning("找不到你的賓果局（可能已被管理者重置），請重新整理後再加入。")
            return
        hap = happened_set(c)
        others = [p for p in all_players(c) if p["pid"] != pid]
        hand = hand_of(c, pid)
        bank = active_questions(c)
        msgs = [dict(r) for r in c.execute("SELECT * FROM messages ORDER BY id DESC LIMIT 8")]

    if "flash" in st.session_state:
        kind, text = st.session_state.pop("flash")
        getattr(st, kind)(text)

    hits, lines, near = analyze(me["board"], hap)
    on_line = {i for ln in lines for i in ln}
    miss = {m for _, m in near}
    fire = bool(near) and not lines

    # BINGO 動畫：自己的連線數增加時播放
    seen = st.session_state.setdefault("seen_lines", len(lines))
    if len(lines) > seen:
        st.markdown(bingo_html(len(lines)), unsafe_allow_html=True)
    st.session_state.seen_lines = len(lines)

    # 新戰報 → 右下角跳通知
    last = st.session_state.setdefault("last_msg", msgs[0]["id"] if msgs else 0)
    for m in reversed([m for m in msgs if m["id"] > last and m["kind"] == "sys"][:3]):
        st.toast(m["text"])
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
                    icon = "⭐ " if i in on_line else "✅ " if hits[i] else ""
                    cols[col].button(icon + md_escape(q), key=f"rbc_{i}_{len_class(q)}{state}", help=q,
                                     on_click=toggle_happened, args=(q, me["name"]), use_container_width=True)
        st.caption("點格子 = 標記這題「發生了」，所有卡上有這題的人會同步打勾；再點一次可取消。")

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


with tab_play:
    play_area()


# ───────── 戰況儀表板 & 留言 ─────────
@st.fragment(run_every=REFRESH_SEC + 1)
def dashboard():
    with db() as c:
        hap = happened_set(c)
        ps = all_players(c)
        hands = {r["pid"]: r["n"] for r in c.execute("SELECT pid, COUNT(*) n FROM hand GROUP BY pid")}
    rows = []
    for p in ps:
        hits, lines, near = analyze(p["board"], hap)
        rows.append((0 if lines else 1 if near else 2, p["bingo_at"] or "~", -sum(hits), p))
    rows.sort(key=lambda x: x[:3])
    m1, m2, m3, m4 = st.columns(4)
    m1.metric("參加人數", len(ps))
    m2.metric("🏆 目前有連線", sum(1 for r in rows if r[0] == 0))
    m3.metric("🔥 聽牌中", sum(1 for r in rows if r[0] == 1))
    m4.metric("✅ 已發生題目", len(hap))
    if not ps:
        st.info("還沒有人加入。")
        return
    cols = st.columns(3)
    for k, (*_, p) in enumerate(rows):
        cols[k % 3].markdown(mini_board_html(p, hap, hands.get(p["pid"], 0)), unsafe_allow_html=True)


@st.fragment(run_every=REFRESH_SEC)
def chat_wall():
    st.markdown("#### 💬 留言牆")
    pid = st.session_state.get("pid")
    with db() as c:
        me = get_player(c, pid) if pid else None
        msgs = [dict(r) for r in c.execute("SELECT * FROM messages ORDER BY id DESC LIMIT 40")]
    if me:
        with st.form("chat_form", clear_on_submit=True, border=False):
            t = st.text_input("留言", max_chars=50, label_visibility="collapsed", placeholder="說點什麼…（50 字內）")
            if st.form_submit_button("送出", use_container_width=True):
                chat(me["name"], t)
        ecols = st.columns(5)
        for e, col in zip(["🔥", "😂", "👀", "🙏", "🎉"], ecols):
            col.button(e, key=f"emo_{e}", on_click=chat, args=(me["name"], e), use_container_width=True)
    else:
        st.caption("加入遊戲後就能留言")
    with st.container(height=560, border=False):
        st.markdown("".join(msg_html(m) for m in msgs) or "還沒有留言", unsafe_allow_html=True)


with tab_dash:
    d1, d2 = st.columns([2.6, 1], gap="large")
    with d1:
        dashboard()
    with d2:
        chat_wall()


# ───────── 管理 ─────────
with tab_admin:
    if not st.session_state.get("admin"):
        pw = st.text_input("管理者密碼", type="password")
        if st.button("登入"):
            if pw == ADMIN_PASSWORD:
                st.session_state.admin = True
                st.rerun()
            st.error("密碼錯誤")
    else:
        if st.button("登出管理"):
            st.session_state.admin = False
            st.rerun()
        with db() as c:
            qdf = pd.DataFrame([dict(r) for r in c.execute("SELECT id, text, active, updated_at FROM questions ORDER BY id")])
            hdf = pd.DataFrame([dict(r) for r in c.execute("SELECT * FROM happened ORDER BY at DESC")],
                               columns=["text", "by_name", "at"])
            players = all_players(c)
            hap = happened_set(c)
            hands_all = {p["pid"]: "、".join(CARDS[h["card"]][1] for h in hand_of(c, p["pid"])) for p in players}

        st.subheader("📚 題庫")
        st.caption("想永久保留的題目請改 GitHub 上的 questions.txt。超過 22 字的題目會自動縮小字體，建議精簡。")
        with st.expander("➕ 新增題目"):
            txt = st.text_area("一行一題")
            if st.button("新增"):
                with db() as c:
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
            with db() as c:
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
            st.dataframe(pd.DataFrame([{"姓名": p["name"], "卡號": p["pid"], "手牌": hands_all[p["pid"]] or "（無）"}
                                       for p in players]), hide_index=True, use_container_width=True)

        st.subheader("🎁 發卡 / 留言 / 重置")
        a, b, cc = st.columns(3)
        if a.button("全員各發一張卡（目前無連線者）"):
            with db() as c:
                got = [p["name"] for p in players if not p["lines"] and give_card(c, p["pid"])]
                post(c, f"🎁 管理者發卡給：{'、'.join(got) or '無（手牌已滿）'}")
            st.rerun()
        if b.button("清空留言"):
            with db() as c:
                c.execute("DELETE FROM messages WHERE kind='chat'")
            st.rerun()
        confirm = cc.checkbox("我確定要重置整場遊戲")
        if cc.button("⚠️ 重置遊戲", disabled=not confirm):
            with db() as c:
                c.executescript("DELETE FROM players; DELETE FROM happened; DELETE FROM hand; DELETE FROM messages;")
            logout()
            st.rerun()

        if players:
            out = pd.DataFrame([{
                "卡號": p["pid"], "姓名": p["name"], "加入時間": p["created_at"],
                "目前連線數": len(analyze(p["board"], hap)[1]), "首次賓果時間": p["bingo_at"] or "",
                "卡片內容": "｜".join(p["board"])} for p in players])
            st.download_button("⬇️ 下載戰況名單（CSV）", out.to_csv(index=False).encode("utf-8-sig"),
                               "bingo_result.csv", "text/csv")
