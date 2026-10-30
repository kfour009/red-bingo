"""紅檜賓果 — Streamlit + SQLite（本機 / Streamlit Community Cloud 皆可）
本機啟動：雙擊 start.bat，或執行  python -m streamlit run app.py
"""
import json
import random
import sqlite3
import uuid
from datetime import datetime
from html import escape
from pathlib import Path
from zoneinfo import ZoneInfo

import pandas as pd
import streamlit as st

# ═════════ 設定 ═════════
GRID = 4
CELLS = GRID * GRID
TZ = ZoneInfo("Asia/Taipei")                       # 雲端主機是 UTC，固定用台北時間
DB_PATH = Path.home() / "red_bingo" / "bingo.db"   # 不放在 OneDrive / GitHub 資料夾內
QFILE = Path(__file__).parent / "questions.txt"    # 題庫來源檔（一行一題）

# 管理密碼：優先讀 Streamlit secrets；本機沒設定時才用預設值
# ⚠️ 上傳 GitHub 前，不要把真正的密碼寫在這裡
try:
    ADMIN_PASSWORD = st.secrets["ADMIN_PASSWORD"]
except Exception:
    ADMIN_PASSWORD = "1234"
# ═══════════════════════

# 4 橫 + 4 直 + 2 斜 = 10 條連線
LINES = ([[r * GRID + c for c in range(GRID)] for r in range(GRID)]
         + [[r * GRID + c for r in range(GRID)] for c in range(GRID)]
         + [[i * GRID + i for i in range(GRID)], [i * GRID + GRID - 1 - i for i in range(GRID)]])


def now() -> str:
    return datetime.now(TZ).strftime("%Y-%m-%d %H:%M:%S")


def file_questions() -> list[str]:
    if not QFILE.exists():
        return []
    return [l.strip() for l in QFILE.read_text(encoding="utf-8-sig").splitlines() if l.strip()]


# ───────── 資料庫 ─────────
def conn():
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    c = sqlite3.connect(DB_PATH, timeout=10)
    c.execute("PRAGMA journal_mode=WAL")
    return c


def init_db():
    with conn() as c:
        c.execute("""CREATE TABLE IF NOT EXISTS questions(
            id INTEGER PRIMARY KEY AUTOINCREMENT, text TEXT UNIQUE NOT NULL,
            active INTEGER NOT NULL DEFAULT 1, updated_at TEXT)""")
        c.execute("""CREATE TABLE IF NOT EXISTS cards(
            card_id TEXT PRIMARY KEY, name TEXT NOT NULL, created_at TEXT NOT NULL,
            questions TEXT NOT NULL, marks TEXT NOT NULL, updated_at TEXT)""")
        # questions.txt 裡新增的題目會自動補進題庫（已存在的不會重複）
        c.executemany("INSERT OR IGNORE INTO questions(text, active, updated_at) VALUES(?,1,?)",
                      [(q, now()) for q in file_questions()])


def q_df():
    with conn() as c:
        return pd.read_sql("SELECT id AS 編號, text AS 題目, active AS 啟用, updated_at AS 修改時間 "
                           "FROM questions ORDER BY id", c)


def cards_df():
    with conn() as c:
        return pd.read_sql("SELECT card_id AS 卡號, name AS 姓名, created_at AS 建立時間, "
                           "questions AS 題目, marks AS 標記, updated_at AS 更新時間 "
                           "FROM cards ORDER BY created_at", c)


def save_card(name, qs):
    cid = "RC-" + uuid.uuid4().hex[:6].upper()
    with conn() as c:
        c.execute("INSERT INTO cards VALUES(?,?,?,?,?,?)",
                  (cid, name, now(), json.dumps(qs, ensure_ascii=False), "0" * CELLS, now()))
    return cid


def parse_marks(s):
    return [ch == "1" for ch in str(s).ljust(CELLS, "0")[:CELLS]]


def bingo_lines(marks):
    return [ln for ln in LINES if all(marks[i] for i in ln)]


def toggle_mark(cid, i):
    with conn() as c:
        row = c.execute("SELECT marks FROM cards WHERE card_id=?", (cid,)).fetchone()
        if not row:
            return
        m = parse_marks(row[0])
        before = len(bingo_lines(m))
        m[i] = not m[i]
        after = len(bingo_lines(m))
        if after > before:  # 新增連線 → 下一次畫面重繪時播放 BINGO 動畫
            st.session_state.bingo_anim = after
        c.execute("UPDATE cards SET marks=?, updated_at=? WHERE card_id=?",
                  ("".join("1" if x else "0" for x in m), now(), cid))


# ───────── 樣式 ─────────
CSS = """
<style>
.stApp {background: radial-gradient(circle at 10% 0%, #FFF3D6 0%, #FFFBF2 45%, #EEF6EF 100%);}
.hero {padding: 18px 24px; border-radius: 22px; margin-bottom: 8px;
  background: linear-gradient(120deg, #2E7D5B, #5BA77F); color: #fff;}
.hero h1 {margin: 0; font-size: 2rem; letter-spacing: 2px; color:#fff;}
.hero p {margin: 4px 0 0; opacity: .9;}
.card-title {background: #FFC000; color: #3a2a00; text-align: center; font-weight: 800;
  font-size: 1.25rem; padding: 10px; border-radius: 16px 16px 0 0;}
.card-meta {text-align: center; font-size: .85rem; color: #6b5d3d; margin: 4px 0 10px;}
.bgrid {display: grid; grid-template-columns: repeat(4, 1fr); gap: 10px;
  background: #fff; padding: 12px; border-radius: 0 0 16px 16px;
  box-shadow: 0 8px 24px rgba(46,125,91,.12);}
.bcell {position: relative; min-height: 96px; border-radius: 14px; background: #FFFDF7;
  border: 2px solid #E9DFC6; display: flex; align-items: center; justify-content: center;
  text-align: center; font-weight: 700; padding: 18px 8px 8px; color:#2B2B2B;
  transition: transform .15s, box-shadow .15s;}
.bcell:hover {transform: translateY(-3px); box-shadow: 0 6px 14px rgba(0,0,0,.08);}
.bcell .no {position: absolute; top: 6px; left: 10px; font-size: .72rem; color: #B49A5E; font-weight: 800;}
.bcell.on {background: #E3F3E8; border-color: #2E7D5B; color: #1E5A40;}
.bcell.on::after {content: "🌲"; position: absolute; top: 4px; right: 8px;}
.bcell.line {background: #FFF1C2; border-color: #FFB000; color: #7A4B00;
  box-shadow: 0 0 0 3px rgba(255,176,0,.35);}
div[data-testid="stButton"] button {min-height: 60px; width: 100%; border-radius: 14px;
  font-weight: 700; white-space: normal; line-height: 1.3;}
div[data-testid="stButton"] button[kind="primary"] {background: #2E7D5B; border-color: #2E7D5B; color:#fff;}
</style>
"""

# 連線進場動畫：全螢幕 → 紅檜長出來 → 標題浮上 → 四個方格彈出 → 淡出
WELCOME = """
<style>
#rb-welcome {position: fixed; inset: 0; z-index: 999999; display: flex; flex-direction: column;
  align-items: center; justify-content: center; pointer-events: none;
  background: linear-gradient(135deg, #2E7D5B, #5BA77F 55%, #FFC000);
  animation: rbOut .8s ease 2.6s forwards;}
#rb-welcome .tree {font-size: 96px; animation: rbGrow 1.2s cubic-bezier(.2,1.6,.4,1) both;}
#rb-welcome h2 {color: #fff; font-size: 2.4rem; margin: 8px 0 0; letter-spacing: 4px;
  animation: rbUp .8s ease .6s both;}
#rb-welcome p {color: #FFF7DA; margin: 6px 0 0; animation: rbUp .8s ease .9s both;}
#rb-welcome .dots span {display: inline-block; width: 14px; height: 14px; margin: 16px 5px 0;
  border-radius: 4px; background: #FFC000; box-shadow: 0 0 0 2px #fff; animation: rbPop .5s ease both;}
#rb-welcome .dots span:nth-child(1){animation-delay:.95s}
#rb-welcome .dots span:nth-child(2){animation-delay:1.1s}
#rb-welcome .dots span:nth-child(3){animation-delay:1.25s}
#rb-welcome .dots span:nth-child(4){animation-delay:1.4s}
@keyframes rbGrow {from {transform: scale(0) rotate(-20deg);} to {transform: scale(1) rotate(0);}}
@keyframes rbUp {from {opacity: 0; transform: translateY(20px);} to {opacity: 1; transform: none;}}
@keyframes rbPop {from {transform: scale(0);} to {transform: scale(1);}}
@keyframes rbOut {to {opacity: 0; visibility: hidden;}}
@media (prefers-reduced-motion: reduce) {
  #rb-welcome {animation-delay: .8s;} #rb-welcome * {animation: none !important;}}
</style>
<div id="rb-welcome"><div class="tree">🌲</div><h2>紅檜賓果</h2><p>連線成功，準備開局！</p>
<div class="dots"><span></span><span></span><span></span><span></span></div></div>
"""


def bingo_html(n_lines: int) -> str:
    """BINGO 連線動畫：字母依序彈跳 + 彩紙落下，約 3.5 秒後自動淡出"""
    uid = uuid.uuid4().hex[:8]  # 每次 id 不同，確保動畫一定重新播放
    colors = ["#FFC000", "#2E7D5B", "#E8505B", "#5BA77F", "#FFFFFF", "#F59E0B"]
    confetti = "".join(
        f'<i style="left:{random.randint(0, 100)}%;background:{random.choice(colors)};'
        f'animation-delay:{random.random() * .8:.2f}s;animation-duration:{random.uniform(2.2, 3.4):.2f}s"></i>'
        for _ in range(70))
    letters = "".join(f'<span style="animation-delay:{k * .12:.2f}s">{ch}</span>'
                      for k, ch in enumerate("BINGO!"))
    return f"""
<style>
#bg-{uid} {{position: fixed; inset: 0; z-index: 999999; pointer-events: none; overflow: hidden;
  display: flex; flex-direction: column; align-items: center; justify-content: center;
  background: radial-gradient(circle, rgba(255,192,0,.55), rgba(46,125,91,.8));
  animation: bgOut .7s ease 3.2s forwards;}}
#bg-{uid} .word span {{display: inline-block; font-size: clamp(56px, 13vw, 150px); font-weight: 900;
  color: #fff; text-shadow: 0 6px 0 #B97800, 0 10px 24px rgba(0,0,0,.35);
  animation: bgJump .7s cubic-bezier(.2,1.8,.4,1) both;}}
#bg-{uid} .sub {{color: #fff; font-size: 1.5rem; font-weight: 800; margin-top: 6px;
  animation: bgFade .6s ease .8s both;}}
#bg-{uid} i {{position: absolute; top: -20px; width: 10px; height: 16px; border-radius: 2px;
  animation-name: bgFall; animation-timing-function: linear; animation-fill-mode: both;}}
@keyframes bgJump {{from {{transform: translateY(-120px) scale(.3); opacity: 0;}}
                    to {{transform: none; opacity: 1;}}}}
@keyframes bgFade {{from {{opacity: 0; transform: translateY(12px);}} to {{opacity: 1; transform: none;}}}}
@keyframes bgFall {{from {{transform: rotate(0);}} to {{top: 105%; transform: rotate(720deg);}}}}
@keyframes bgOut {{to {{opacity: 0; visibility: hidden;}}}}
@media (prefers-reduced-motion: reduce) {{#bg-{uid} i, #bg-{uid} span {{animation: none !important;}}}}
</style>
<div id="bg-{uid}">{confetti}<div class="word">{letters}</div>
<div class="sub">🌲 第 {n_lines} 條連線達成！</div></div>
"""


def grid_html(name, qs, marks=None, meta=""):
    marks = marks or [False] * CELLS
    on_line = {i for ln in bingo_lines(marks) for i in ln}
    cells = "".join(f'<div class="bcell {"on" if m else ""} {"line" if i in on_line else ""}">'
                    f'<span class="no">{i+1:02d}</span>{escape(q)}</div>'
                    for i, (q, m) in enumerate(zip(qs, marks)))
    meta = f'<div class="card-meta">{escape(meta)}</div>' if meta else ""
    return f'<div class="card-title">{escape(name)} 的紅檜賓果</div><div class="bgrid">{cells}</div>{meta}'


# ───────── 頁面 ─────────
st.set_page_config(page_title="紅檜賓果", page_icon="🌲", layout="wide")
st.markdown(CSS, unsafe_allow_html=True)
init_db()

# 每次有人連線（開啟頁面 / 重新整理）播放一次，之後按按鈕不會重播
if not st.session_state.get("welcomed"):
    st.markdown(WELCOME, unsafe_allow_html=True)
    st.session_state.welcomed = True

# 有新的賓果連線時播放 BINGO 動畫（每條新連線播一次）
if "bingo_anim" in st.session_state:
    st.markdown(bingo_html(st.session_state.pop("bingo_anim")), unsafe_allow_html=True)

st.markdown('<div class="hero"><h1>🌲 紅檜賓果</h1><p>輸入名字 → 換題目 → 下好離手。一格一話題，看誰先連線！</p></div>',
            unsafe_allow_html=True)

qdf, cdf = q_df(), cards_df()
active_q = list(qdf.loc[qdf["啟用"] == 1, "題目"])

with st.sidebar:
    st.header("📊 即時狀況")
    a, b = st.columns(2)
    a.metric("啟用題目", len(active_q))
    b.metric("已鎖定卡片", len(cdf))
    st.metric("參加人數", cdf["姓名"].nunique())
    st.subheader("最新鎖定")
    for _, r in cdf.tail(5).iloc[::-1].iterrows():
        st.markdown(f"**{r['姓名']}**　`{r['卡號']}`  \n{r['建立時間']}")
    if cdf.empty:
        st.caption("還沒有人下好離手")
    if st.button("🔄 重新整理", use_container_width=True):
        st.rerun()

tab_play, tab_list, tab_bank = st.tabs(["🎯 我的賓果", "📋 賓果名單", "🔒 題庫管理"])

with tab_play:
    left, right = st.columns([1, 2.4], gap="large")
    with left:
        name = st.text_input("請輸入您的名字", placeholder="例如：PEYTON").strip()
        short = len(active_q) < CELLS
        if short:
            st.warning(f"啟用題目只有 {len(active_q)} 題，至少需要 {CELLS} 題，請聯絡管理者。")
        if st.button("🎲 換題目", disabled=short or not name):
            st.session_state.draft = random.sample(active_q, CELLS)
        ok = st.checkbox("我確認這張卡，鎖定後不能再換", disabled="draft" not in st.session_state)
        if st.button("✋ 下好離手", type="primary",
                     disabled=not (name and ok and "draft" in st.session_state)):
            st.session_state.last_card = save_card(name, st.session_state.pop("draft"))
            st.balloons()
            st.rerun()
        if not name:
            st.caption("先輸入名字才能換題目喔")
    with right:
        if "draft" in st.session_state and name:
            st.markdown(grid_html(name, st.session_state.draft, meta="草稿：尚未鎖定，可以繼續換題目"),
                        unsafe_allow_html=True)
        else:
            st.info("輸入名字後按「換題目」產生你的賓果卡。")

    st.divider()
    st.subheader("🖐️ 我的卡片（點格子標記 / 取消）")
    mine = cdf[cdf["姓名"] == name] if name else cdf.iloc[0:0]
    if mine.empty:
        st.caption("輸入名字並下好離手後，你的卡片會出現在這裡。")
    else:
        ids = mine["卡號"].tolist()[::-1]
        idx = ids.index(st.session_state["last_card"]) if st.session_state.get("last_card") in ids else 0
        cid = st.selectbox("選擇卡片", ids, index=idx,
                           format_func=lambda x: f"{x}（{mine.loc[mine['卡號']==x,'建立時間'].iloc[0]}）")
        row = mine[mine["卡號"] == cid].iloc[0]
        qs, marks = json.loads(row["題目"]), parse_marks(row["標記"])
        on_line = {k for ln in bingo_lines(marks) for k in ln}
        st.markdown(f'<div class="card-title">{escape(name)} 的紅檜賓果　<small>{cid}</small></div>',
                    unsafe_allow_html=True)
        for r in range(GRID):
            cols = st.columns(GRID)
            for c in range(GRID):
                i = r * GRID + c
                icon = "⭐ " if i in on_line else ("🌲 " if marks[i] else f"{i+1:02d}｜")
                cols[c].button(icon + qs[i], key=f"m_{cid}_{i}",
                               type="primary" if marks[i] else "secondary",
                               on_click=toggle_mark, args=(cid, i), use_container_width=True)
        st.caption(f"已標記 {sum(marks)} / {CELLS} 格｜🎉 連線 {len(bingo_lines(marks))} 條（⭐ 為連線格）")

with tab_list:
    st.subheader("所有已鎖定的賓果卡")
    if cdf.empty:
        st.info("目前還沒有卡片。")
    else:
        kw = st.text_input("🔍 搜尋姓名或卡號").strip()
        v = cdf.copy()
        v["已標記"] = v["標記"].apply(lambda s: sum(parse_marks(s)))
        v["連線數"] = v["標記"].apply(lambda s: len(bingo_lines(parse_marks(s))))
        if kw:
            v = v[v["姓名"].str.contains(kw, case=False, regex=False) |
                  v["卡號"].str.contains(kw, case=False, regex=False)]
        st.dataframe(v[["卡號", "姓名", "建立時間", "已標記", "連線數", "更新時間"]].iloc[::-1],
                     hide_index=True, use_container_width=True)
        if not v.empty:
            pick = st.selectbox("查看卡片內容（兌獎查證）", v["卡號"].tolist()[::-1],
                                format_func=lambda x: f"{x}｜{v.loc[v['卡號']==x,'姓名'].iloc[0]}")
            r = v[v["卡號"] == pick].iloc[0]
            st.markdown(grid_html(r["姓名"], json.loads(r["題目"]), parse_marks(r["標記"]),
                                  meta=f"卡號 {pick}｜建立 {r['建立時間']}｜最後標記 {r['更新時間']}"),
                        unsafe_allow_html=True)

with tab_bank:
    if not st.session_state.get("admin"):
        pw = st.text_input("管理者密碼", type="password")
        if st.button("登入"):
            if pw == ADMIN_PASSWORD:
                st.session_state.admin = True
                st.rerun()
            else:
                st.error("密碼錯誤")
        st.caption("題庫僅限管理者維護。")
    else:
        st.success("已登入管理者")
        if st.button("登出"):
            st.session_state.admin = False
            st.rerun()

        st.info("💡 想永久保留的題目，請改 GitHub 上的 questions.txt；"
                "這裡的修改在雲端主機重啟後可能會消失。")

        with st.expander("➕ 新增題目"):
            txt = st.text_area("一行一題，可一次貼多題")
            if st.button("新增"):
                items = [t.strip() for t in txt.splitlines() if t.strip()]
                n = 0
                with conn() as c:
                    for t in items:
                        n += c.execute("INSERT OR IGNORE INTO questions(text,active,updated_at) VALUES(?,1,?)",
                                       (t, now())).rowcount
                st.success(f"已新增 {n} 題（重複的會自動略過）")
                st.rerun()

        st.caption("可直接改題目文字，或取消「啟用」停用題目（已鎖定的卡片不受影響）。")
        e = qdf.copy()
        e["啟用"] = e["啟用"] == 1
        ed = st.data_editor(e, hide_index=True, use_container_width=True, num_rows="fixed",
                            disabled=["編號", "修改時間"],
                            column_config={"題目": st.column_config.TextColumn(width="large", required=True)})
        if st.button("💾 儲存變更", type="primary"):
            n = 0
            with conn() as c:
                for (_, o), (_, w) in zip(e.iterrows(), ed.iterrows()):
                    t = str(w["題目"]).strip() or o["題目"]
                    if t != o["題目"] or bool(w["啟用"]) != bool(o["啟用"]):
                        try:
                            c.execute("UPDATE questions SET text=?, active=?, updated_at=? WHERE id=?",
                                      (t, int(bool(w["啟用"])), now(), int(o["編號"])))
                            n += 1
                        except sqlite3.IntegrityError:
                            st.warning(f"「{t}」與其他題目重複，未更新")
            st.success(f"已更新 {n} 題")
            st.rerun()

        st.divider()
        st.download_button("⬇️ 下載賓果名單（CSV，建議活動結束立刻備份）",
                           cdf.to_csv(index=False).encode("utf-8-sig"), "bingo_cards.csv", "text/csv")
