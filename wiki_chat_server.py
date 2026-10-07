#!/usr/bin/env python3
"""
Qcells EMS 위키봇 — 라이브 Confluence 검색을 배경지식으로 쓰는 ChatGPT 스타일 웹 챗봇 서버.
(로컬 위키 docs/*.md는 답변 소스에서 제외 — 사용자 확정, 느슨하게만 연관된 범용
아키텍처 문서가 섞여 들어가 답변 품질을 해쳤음)

사용법: python3 wiki_chat_server.py [--port 8010]
접속:  http://localhost:8010  (WSL2 -> Windows 브라우저 자동 포워딩)
"""

import os
import io
import re
import sys
import ssl
import html
import json
import uuid
import time
import queue
import threading
import base64
import shutil
import smtplib
import zipfile
import subprocess
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from pathlib import Path
from datetime import datetime, timedelta
from email.message import EmailMessage

from flask import Flask, Response, request, jsonify, send_from_directory, session, redirect, url_for, render_template_string

sys.path.insert(0, str(Path(__file__).parent))
from search_query_utils import _clean_query, _tech_query, _extract_terms, _KO_STOP, _apply_person_aliases  # CQL 검색어 정제용 헬퍼만 재사용 (로컬 위키 검색 자체는 미사용)
from query_expansion import _expand_search_query, retry_person_extraction  # claude -p 검색어 확장 프롬프트 엔지니어링 전담 모듈
from usage_guard import (
    is_usage_exhausted, mark_usage_exhausted, looks_like_usage_limit_error, USAGE_EXHAUSTED_MESSAGE,
)  # claude -p 사용량 한도 소진 감지 전담 모듈
from confluence_to_text import render as render_storage_html
from bs4 import BeautifulSoup
from atlassian_mcp_client import (
    rovo_search, search_by_creator, find_author_id_by_title, lookup_cached_person_in_text,
    get_energysw_roster, _ROSTER_PAGE_TITLE, SITE_URL as ATLASSIAN_SITE,
    _EMPTY_BODY_NOTE, _confluence_space_of, CONFLUENCE_PERSONAL_SPACE_OWNERS,
)
from jira_client import search_jira_assigned  # Jira 연동 전담 모듈(atlassian_mcp_client.py 상단 주석 참고)
import wiki_chat_history as chat_history
from run_trace import Tracer  # 작업대(파이프라인 단계 실시간 표시/실행 기록) 이벤트 수집
import model_arena  # 여러 모델 답변 비교 → 채택
import file_store  # 업로드 파일 저장/텍스트 추출

BASE_DIR = Path(__file__).parent
STATIC_DIR = BASE_DIR / "static"
DOCS_IMAGES_DIR = BASE_DIR / "docs" / "images"
LIVE_DIAGRAM_DIR = BASE_DIR / ".confluence_live_images"
LIVE_DIAGRAM_DIR.mkdir(exist_ok=True)
ENV_PATH = BASE_DIR / ".env.confluence"
# EnergySW로만 좁혀서 검색하면 다른 스페이스(MAG 등)에 있는 실제 관련 문서를
# 통째로 못 찾는다 — 실측: "Advanced TOU - TimeTable 로직 및 확인사항"은 MAG 스페이스.
# confluence_export_multi.py가 이미 export해본 스페이스 전체를 검색 대상으로 삼는다.
# GSP("Development PM", 현행)/DP1("Development PM (old)", 구버전) 추가 사유는
# atlassian_mcp_client.py의 동일 리스트 주석 참고 (2026-09-11, 사용자 확정).
CONFLUENCE_TEAM_SPACES = ["EnergySW", "ACGEN2", "CWS", "GDRI", "MAG", "HP", "SIACS", "GSP1", "GSP", "DP1"]

# 개인 스페이스(팀 스페이스와 별도 관리 — atlassian_mcp_client.py와 동일하게 유지).
# 원래 이 파일에는 AhyoungKim 추가분이 누락돼 있었음 — 2026-09-11 동기화하며 같이 반영.
CONFLUENCE_PERSONAL_SPACES = [
    "~712020fbdcf344af074f33bf0d76cfe893cd15",  # AhyoungKim
    "~63c74eb4e28ec74364cc217b",  # Hayool Kim
]

CONFLUENCE_SPACES = CONFLUENCE_TEAM_SPACES + CONFLUENCE_PERSONAL_SPACES
_CQL_SPACE_CLAUSE = "space in (" + ", ".join(f'"{s}"' for s in CONFLUENCE_SPACES) + ")"

app = Flask(__name__)
app.secret_key = os.environ.get("ADMIN_SECRET_KEY") or os.urandom(32)
# 관리자 로그인 세션을 명시적으로 3시간짜리 영구 쿠키로 만든다(2026-09-22, 사용자 요청 —
# **관리자 전용**, 일반 사용자 로그인/비로그인은 시간제한 없이 유지하도록 별도로 확정됨).
# session.permanent를 안 켜면 Flask는 만료 시각이 없는 "브라우저 세션 쿠키"를 내려보내는데,
# 일부 브라우저/탭 환경에서 이게 페이지 이동만으로도 조용히 사라지는 것처럼 보이는 문제(실측:
# 관리자 로그인 후 "비로그인" 메뉴 클릭 시 로그아웃된 것처럼 보임)가 있었다. Max-Age가 박힌
# 쿠키로 바꾸면 "로그아웃 누르거나 3시간 지날 때까지 유지"가 명확해진다. admin_login()에서만
# session.permanent = True를 켜므로 이 설정도 관리자 세션에만 적용된다.
app.config["PERMANENT_SESSION_LIFETIME"] = timedelta(hours=3)
app.config["MAX_CONTENT_LENGTH"] = 25 * 1024 * 1024  # 파일 업로드(file_store.MAX_UPLOAD_BYTES=20MB + 여유)

# 화면 우상단/랜딩에 찍히는 버전 배지(2026-09-18 전엔 static/index.html에 "v1.2.2"로 하드코딩
# 돼 있어서 배포 때마다 HTML을 직접 고쳐야 했다 — 사용자 요청으로 wikibot.env의
# WIKIBOT_VERSION 값을 /api/whoami로 내려서 프론트가 그대로 표시하게 바꿨다. 릴리스마다
# wikibot.env만 갱신하면 되고(코드 수정 불필요), 값이 없으면 "dev"로 표시해 로컬 개발
# 인스턴스와 정식 배포본을 한눈에 구분할 수 있게 한다.
WIKIBOT_VERSION = os.environ.get("WIKIBOT_VERSION", "dev")

# --admin 플래그로 띄운 인스턴스는 일반 인스턴스와 같은 UI/기능을 쓰되 (1) 로그인이 걸리고
# (2) 소프트 삭제된 대화도 필터링하지 않는다.
ADMIN_MODE = "--admin" in sys.argv
ADMIN_USERNAME = os.environ.get("ADMIN_USERNAME", "")
ADMIN_PASSWORD_HASH = os.environ.get("ADMIN_PASSWORD_HASH", "")

_ADMIN_LOGIN_EXEMPT_PATHS = {"/login", "/health"}

# 관리자 인스턴스(18011)는 공용 인스턴스(8010)와 다른 이름의 세션 쿠키를 쓴다(2026-10-07 버그 수정).
# 브라우저 쿠키는 포트를 구분하지 않아서, 둘 다 기본 이름("session")을 쓰면 같은 PC에서 관리자 페이지에
# 로그인하는 순간 공용 화면의 쿠키(비로그인 anon_id/개인 로그인)를 덮어쓰고, 관리자 로그아웃
# (session.clear())이 그것마저 지워버린다. 실측: 관리자 로그인→로그아웃 직후 공용 화면에서 보던 대화가
# 404가 되고, 이어서 보낸 질문이 새 작업으로 따로 저장됨("이전 대화기록이 삭제됐어"로 보고됨).
if ADMIN_MODE:
    app.config["SESSION_COOKIE_NAME"] = "wikibot_admin_session"

_ADMIN_LOGIN_PAGE = """
<!doctype html>
<html lang="ko"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>위키봇 관리자 로그인</title>
<script>try{var t=localStorage.getItem('theme');if(t==='light'||t==='dark')document.documentElement.setAttribute('data-theme',t)}catch(e){}</script>
<link rel="stylesheet" href="https://cdn.jsdelivr.net/gh/orioncactus/pretendard@v1.3.9/dist/web/variable/pretendardvariable-dynamic-subset.min.css">
<link rel="stylesheet" href="/static/app.css">
<style>
  body { display: flex; align-items: center; justify-content: center; min-height: 100vh; padding: 16px; }
  .login-card { width: min(360px, 100%); }
  .login-card .brand { margin-bottom: 18px; }
  .login-card h1 { margin: 0 0 4px; font-size: 22px; letter-spacing: -.035em; }
  .login-card p { margin: 0 0 18px; color: var(--fg-muted); font-size: 13px; }
  .login-card .btn { width: 100%; margin-top: 6px; }
</style></head>
<body>
  <form method="post" class="modal login-card">
    <div class="brand"><span>Q</span><strong>cells EMS Wiki</strong><small>admin</small></div>
    <h1>관리자 로그인</h1>
    <p>전체 대화와 실행 기록을 보는 관리자 화면입니다.</p>
    {% if error %}<div class="form-status" style="margin-bottom:10px">{{ error }}</div>{% endif %}
    <div class="field"><label>아이디</label><input name="username" autofocus autocomplete="username"></div>
    <div class="field"><label>비밀번호</label><input name="password" type="password" autocomplete="current-password"></div>
    <button type="submit" class="btn solid">로그인</button>
  </form>
</body></html>
"""


def _check_admin_credentials(username, password):
    from werkzeug.security import check_password_hash

    if not ADMIN_USERNAME or not ADMIN_PASSWORD_HASH:
        return False
    return username == ADMIN_USERNAME and check_password_hash(ADMIN_PASSWORD_HASH, password)


if ADMIN_MODE:
    @app.before_request
    def _require_admin_login():
        if (request.path in _ADMIN_LOGIN_EXEMPT_PATHS or request.path.startswith("/confluence-images/")
                or request.path.startswith("/wiki-images/") or request.path.startswith("/static/")):
            return None
        if not session.get("admin"):
            return redirect(url_for("admin_login", next=request.path))
        return None

    @app.route("/login", methods=["GET", "POST"])
    def admin_login():
        error = None
        if request.method == "POST":
            if _check_admin_credentials(request.form.get("username", ""), request.form.get("password", "")):
                session.permanent = True
                session["admin"] = True
                return redirect(request.args.get("next") or url_for("admin_home"))
            error = "아이디 또는 비밀번호가 올바르지 않습니다."
        return render_template_string(_ADMIN_LOGIN_PAGE, error=error)

    @app.route("/logout", methods=["POST"])
    def admin_logout():
        session.clear()
        return redirect(url_for("admin_login"))

    # 관리자 대시보드 — 2026-09-22에 Jinja 서버 렌더링 페이지(사용자/비로그인/대화 목록/상세)로
    # 처음 만들었다가, 2026-10-07 "전체 기록" 목록 + 기록 상세(대화 전문·처리 과정 재생) 구성의
    # static/admin.html 단일 페이지로 다시 만들었다. 서버는 JSON만 내려주고,
    # 화면은 대화 화면과 같은 static/app.css·markdown.js를 공유한다. 이제 대화마다 실행 기록
    # (runs — 검색 단계, 모델 비교·채택 판정)까지 같이 보이므로 "누가 무엇을 물었고 봇이 어떤
    # 과정을 거쳐 답했는지"를 한 화면에서 재생할 수 있다.
    @app.route("/admin")
    def admin_home():
        return send_from_directory(STATIC_DIR, "admin.html")

    # 예전 Jinja 화면 주소로 들어와도 새 화면의 해당 탭으로 보낸다(북마크 호환).
    @app.route("/admin/users")
    def admin_users():
        return redirect("/admin#/users")

    @app.route("/admin/anonymous")
    def admin_anonymous():
        return redirect("/admin#/records?owner=anon")

    @app.route("/admin/conversations/<conversation_id>")
    def admin_conversation_detail(conversation_id):
        return redirect(f"/admin#/history/{conversation_id}")

    @app.route("/admin/api/overview")
    def admin_api_overview():
        convs = chat_history.list_conversations_overview()
        for c in convs:
            c["owner"] = c["username"] or (
                "예전 대화(계정 도입 전)" if c["anon_session_id"] == "legacy-pre-login"
                else f"비로그인 {(c['anon_session_id'] or '')[:8]}"
            )
            c["owner_kind"] = "user" if c["user_id"] else "anon"
        return jsonify({"conversations": convs, "users": chat_history.list_users()})

    @app.route("/admin/api/conversations/<conversation_id>")
    def admin_api_conversation(conversation_id):
        conv = chat_history.get_conversation(conversation_id, include_deleted=True)
        if conv is None:
            return jsonify({"error": "대화를 찾을 수 없습니다."}), 404
        owner = None
        if conv.get("user_id"):
            u = chat_history.get_user_by_id(conv["user_id"])
            owner = u["username"] if u else f"삭제된 계정 #{conv['user_id']}"
        conv["owner"] = owner or f"비로그인 {(conv.get('anon_session_id') or '')[:8]}"
        conv["runs"] = chat_history.list_runs_for_conversation(conversation_id)
        return jsonify(conv)

    @app.route("/admin/api/users/<int:user_id>/delete", methods=["POST"])
    def admin_api_user_delete(user_id):
        chat_history.delete_user(user_id)
        return jsonify({"ok": True})

# 일반 사용자 계정 로그인(공용 인스턴스 전용, 2026-09-22 신규) — ADMIN_MODE의 관리자
# 세션(session["admin"])과는 완전히 별개 키(session["user_id"]/["username"])를 쓴다.
# 비로그인 방문자도 "이 브라우저 세션 동안의 내 대화" 사이드바를 가지도록 매 요청마다
# anon_id(세션 쿠키에 저장되는 uuid)를 발급해 conversations.anon_session_id로 묶는다 —
# 로그인 없이도 개인 대화 목록처럼 보이되, 다른 비로그인 방문자의 대화와는 안 섞인다.
if not ADMIN_MODE:
    @app.before_request
    def _ensure_anon_id():
        # 로그인 세션과 달리 시간제한을 두지 않는다(사용자 확정, 2026-09-22) — session.permanent를
        # 켜면 앱 전역 PERMANENT_SESSION_LIFETIME(3시간, 로그인용)을 그대로 물려받아 비로그인
        # 대화도 3시간 뒤 끊기게 되므로 일부러 안 켬. 예전에 "새로고침하면 대화가 사라진다"고
        # 느꼈던 원인은 이 플래그가 아니라 서버 재시작마다 서명키(ADMIN_SECRET_KEY)가 랜덤으로
        # 바뀌어 쿠키가 무효화된 것이었고, 그건 이미 고정 키 사용으로 해결됨 — 여기는 원래대로
        # "브라우저가 쿠키를 지우기 전까지" 유지되는 무기한 세션 쿠키로 되돌린다.
        if "user_id" not in session and "anon_id" not in session:
            session["anon_id"] = uuid.uuid4().hex

    def _current_identity():
        """(user_id, anon_session_id) 튜플. 로그인 상태면 anon_session_id는 항상 None."""
        user_id = session.get("user_id")
        if user_id:
            return user_id, None
        return None, session.get("anon_id")

    def _owns_conversation(conv):
        user_id, anon_id = _current_identity()
        if user_id:
            return conv.get("user_id") == user_id
        return conv.get("anon_session_id") == anon_id

    @app.route("/api/register", methods=["POST"])
    def register():
        body = request.get_json(force=True) or {}
        username = (body.get("username") or "").strip()
        password = body.get("password") or ""
        if len(username) < 3:
            return jsonify({"error": "아이디는 3자 이상이어야 합니다."}), 400
        if len(password) < 4:
            return jsonify({"error": "비밀번호는 4자 이상이어야 합니다."}), 400
        try:
            user_id = chat_history.create_user(username, password)
        except chat_history.UsernameTaken:
            return jsonify({"error": "이미 사용 중인 아이디입니다."}), 409
        session.clear()
        # 3시간 제한은 관리자 로그인 전용(admin_login() 참고) — 일반 사용자 로그인은 비로그인
        # 세션과 마찬가지로 시간제한 없이 유지한다(사용자 확정, 2026-09-22).
        session["user_id"] = user_id
        session["username"] = username
        return jsonify({"ok": True, "username": username})

    @app.route("/api/login", methods=["POST"])
    def user_login():
        body = request.get_json(force=True) or {}
        username = (body.get("username") or "").strip()
        password = body.get("password") or ""
        user = chat_history.get_user_by_username(username)
        if not user or user["password"] != password:
            return jsonify({"error": "아이디 또는 비밀번호가 올바르지 않습니다."}), 401
        session.clear()
        session["user_id"] = user["id"]
        session["username"] = user["username"]
        return jsonify({"ok": True, "username": user["username"]})

    @app.route("/api/logout", methods=["POST"])
    def user_logout():
        session.clear()
        return jsonify({"ok": True})

SYSTEM_PROMPT = """당신은 "Qcells EMS 위키봇"입니다. QCells EMS(Energy Management System) 팀의
Confluence 문서를 배경지식으로 삼아 답하는 개발 어시스턴트입니다.

답변 규칙:
- 아래 제공된 "참고 자료" 안의 내용만 근거로 답하세요. 참고 자료에 없으면 "위키/Confluence에서 해당 내용을 찾지 못했습니다"라고 말하세요
- 참고 자료의 출처 표시에 "~~님의 개인 Confluence 스페이스 문서"라고 적혀 있으면, 그 문서는 본문에
  그 사람 이름이 안 적혀 있어도 그 사람이 직접 작성한 본인 업무 노트입니다. "이름이 본문에
  없어서 누구인지 모른다"고 답하지 말고, 그 문서 내용(다루는 주제/프로젝트/기술)을 "이 분이
  다뤄온 업무"로 제시하세요 — 예: "OO님 개인 스페이스에 Generator/HUB 연동 관련 기술노트가
  있어 해당 분야 업무를 맡고 있는 것으로 보입니다." 인물의 직책/소속까지는 이 노트만으로
  확정할 수 없으면 그 점은 솔직히 밝히되, 노트 자체의 존재와 주제는 답변에 반드시 활용하세요
- 출처 표시에 "~~님이 작성한 문서(짧은 발췌)"라고 적혀 있으면, Confluence 메타데이터(작성자)로
  찾아낸 그 사람의 다른 문서들입니다 — 발췌가 짧아서 그 사람 이름이 본문에 안 보여도, 그
  문서의 제목과 다루는 주제 자체가 "이 사람이 이런 업무/프로젝트에 관여했다"는 근거입니다
  (예: 예산 품의서에 그 사람이 결재자로 있거나, 회의록의 작성자로 되어 있는 식). 인물의
  역할/이력을 묻는 질문에는 이런 문서들의 제목과 주제를 적극 종합해서 답에 반영하세요 —
  본문 발췌가 짧다는 이유로 무시하거나 "본문에 이름이 없어 확인 불가"라고 하지 마세요.
  여러 개가 있으면 시기별/주제별로 묶어서 그 사람이 다뤄온 업무 범위를 설명하는 데 쓰세요
- 참고 자료 문서 전체를 요약/나열하지 말고, 사용자 질문에 답하는 데 필요한 내용만 골라서 답하세요.
  특히 참고 자료가 PRD/FRD처럼 문서 전체를 다루는 경우, Role별 권한표(예: "Qcells Admin",
  "Fleet Partner Admin" 같은 웹 콘솔 접근 권한)나 웹/클라우드 콘솔 메뉴 이동 경로(예: "GNB 검색 →
  Edit Site → Post-Commissioning") 같은 절은 사용자가 웹 UI 사용법이나 권한 체계를 직접 묻지 않는
  한 답변에 옮기지 마세요 — 임베디드 EMS 자체의 동작을 묻는 질문에는 무관한 내용입니다
- 코드·함수명·설정값은 참고 자료의 표현을 그대로 인용하세요
- 한국어로 답변하되 기술 용어는 원문 그대로 사용하세요
- 이전 대화 맥락을 참고해서 자연스럽게 이어서 답하세요
- 참고 자료 안의 표에서 인원/항목을 세거나 전체를 나열해 달라는 질문을 받으면, 표의 마지막 행까지
  전부 훑은 뒤에 답하세요. 표 중간에 그룹 라벨(예: "Energy Control & Monitoring")이 첫 행에만
  적혀있고 이후 행은 비어있는 형태(병합된 셀)라도, 그 그룹 라벨은 다음 그룹 라벨이 나오기 전까지
  이어지는 모든 행에 적용됩니다 — 라벨이 안 보인다고 그 행을 건너뛰거나 이전 행에서 끊긴 것으로
  오해하지 마세요. 특히 표가 길면 뒤쪽 행을 놓치기 쉬우니, 개수를 답하기 전에 실제로 하나씩 세어서
  일치하는지 스스로 검증하세요
- 참고 자료 안에 `![설명](images/파일명)` 형식의 이미지 참조가 있으면, 그 마크다운 이미지 구문을
  그대로 답변에 포함하세요 (경로를 바꾸거나 지어내지 마세요. 참고 자료에 없는 이미지를 있는 것처럼
  언급하는 것은 절대 금지)
- 사용자가 그림/다이어그램을 요청했는데 참고 자료에 실제 이미지가 없으면, 대신 ```mermaid 코드
  블록으로 개념을 요약하는 다이어그램(플로우차트, 시퀀스, 타임라인 등 적절한 형태)을 직접
  생성해서 보여주세요. 이때는 답변에 "위키에 저장된 그림은 없어 아래처럼 요약 다이어그램을
  생성했습니다"라고 명시하세요
- mermaid 코드 작성 시 반드시 지킬 것 (안 지키면 파싱 에러로 다이어그램이 아예 안 나옴):
  - 한 줄에 관계(엣지) 하나만 작성. "A --> B(라벨) -|c| D" 처럼 한 줄에서 새 노드를
    정의하면서 동시에 다음 화살표로 이어 붙이지 말 것
  - 노드 라벨에 괄호/특수문자가 들어가면 큰따옴표로 감쌀 것: A["시작 시간(HH:mm)"]
  - 엣지 라벨은 반드시 `-->|라벨|` 형태만 사용 (`-|`, `~` 같은 변형 문법 금지)
  - 예시(이 패턴만 따라 쓰면 안전함):
    ```mermaid
    graph TD;
      A[TOU 스케줄] --> B[시작 시간];
      A --> C[종료 시간];
      B -->|적용| D[기본 동작];
      C -->|적용| D;
    ```
- 답변 마지막에 참고한 출처를 나열할 때, 각 참고 자료 블록 첫 줄에 있는 실제 URL을 그대로 써서
  마크다운 하이퍼링크 `[출처명](URL)` 형식으로 작성하세요. `[[출처명]]`처럼 URL 없는 이중 대괄호
  형식은 클릭할 수 없으니 쓰지 마세요. 참고 자료에 없는 URL을 지어내는 것도 절대 금지입니다
- 출처가 "업로드 파일"인 참고 자료는 사용자가 이 대화에 직접 첨부한 파일입니다. 질문이 그 파일(로그,
  설정, 표, 문서 등)에 관한 것이면 위키 검색 결과보다 첨부 파일을 우선 근거로 삼으세요. 첨부 파일은
  링크가 없으므로 출처는 `(첨부: 파일명)`처럼 이름만 적으세요"""


def _load_confluence_env():
    if not ENV_PATH.exists():
        return None
    env = {}
    for line in ENV_PATH.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        env[k] = v
    return env


_CONF_ENV = _load_confluence_env()


def _cql_search(base, headers, cql, limit, timeout):
    params = urllib.parse.urlencode({
        "cql": cql,
        "limit": limit,
        "expand": "body.storage,version",
    })
    url = f"{base}/wiki/rest/api/content/search?{params}"
    req = urllib.request.Request(url, headers=headers)
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.load(resp)


def _slugify(s):
    return re.sub(r'[\\/:*?"<>|]', '_', s).strip()[:150]


def _get_attachments(page_id, headers, base, att_cache, timeout):
    if page_id in att_cache:
        return att_cache[page_id]
    url = f"{base}/wiki/rest/api/content/{page_id}/child/attachment?limit=100"
    try:
        req = urllib.request.Request(url, headers=headers)
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            atts = json.load(resp).get("results", [])
    except Exception as e:
        print(f"⚠️  첨부파일 조회 실패 pageId={page_id}: {e}", file=sys.stderr)
        atts = []
    att_cache[page_id] = atts
    return atts


def _download_attachment(match, headers, base, dest, timeout):
    dl_path = match["_links"]["download"]
    dl_url = f"{base}{dl_path}" if dl_path.startswith("/wiki") else f"{base}/wiki{dl_path}"
    # Accept: application/json은 JSON API 호출용 — 바이너리 다운로드에는 빼는 게 안전
    dl_headers = {k: v for k, v in headers.items() if k.lower() != "accept"}
    req = urllib.request.Request(dl_url, headers=dl_headers)
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        dest.write_bytes(resp.read())


_DRAWIO_VERSION_RE = re.compile(r'v(\d+)\.(\d+)', re.IGNORECASE)
_HTML_TAG_RE = re.compile(r'<[^>]+>')
_PAGE_ID_IN_URL_RE = re.compile(r'pageId=(\d+)')


def _clean_drawio_label(raw):
    text = html.unescape(raw or "")
    text = _HTML_TAG_RE.sub(' ', text)
    return re.sub(r'\s+', ' ', text).strip()


def _abs_pos(cid, geoms, cache, depth=0):
    """draw.io 자식 도형의 x/y는 부모(group/container)의 로컬 좌표계 기준 상대값이라,
    부모 체인을 타고 올라가며 더해야 캔버스 절대 좌표가 나온다. 이걸 안 하면(단순히
    mxCell을 문서 순서나 raw y로만 정렬하면) 그룹 안에 있는 도형들의 순서가 뒤섞인다
    (실측: 그룹으로 묶인 Install Data 단계가 앞뒤 라벨과 뒤죽박죽으로 나옴)."""
    if cid in cache or depth > 30:
        return cache.get(cid, (0.0, 0.0))
    if cid not in geoms:
        cache[cid] = (0.0, 0.0)
        return cache[cid]
    x, y, parent = geoms[cid]
    if parent:
        px, py = _abs_pos(parent, geoms, cache, depth + 1)
        result = (x + px, y + py)
    else:
        result = (x, y)
    cache[cid] = result
    return result


def _parse_drawio_sequence(xml_bytes):
    """draw.io 시퀀스 다이어그램 XML(mxGraphModel)에서 도형/화살표 텍스트 라벨을
    절대 y좌표(위→아래) 순으로 뽑아 평문 목록으로 재구성한다. draw.io 파일은 압축 없는
    순수 XML이라(실측 확인: mxfile을 열어보면 바로 <mxGraphModel> 텍스트) 별도
    디코딩 없이 표준 라이브러리 xml.etree만으로 파싱 가능 — vision/OCR 불필요."""
    root = ET.fromstring(xml_bytes)
    # 파일 하나 안에 여러 <diagram> "페이지"가 들어있는 경우가 있다(실측: 이 파일 자체가
    # v1.06/v1.05 두 페이지를 담고 있어, root 전체를 훑으면 v1.05가 v1.06과 겹쳐서
    # 거의 모든 라벨이 두 번씩 나옴). 버전 표기가 가장 높은 페이지 하나만 쓴다.
    diagrams = root.findall("diagram")
    if diagrams:
        def diagram_version(d):
            m = _DRAWIO_VERSION_RE.search(d.get("name", ""))
            return (int(m.group(1)), int(m.group(2))) if m else (0, 0)
        scope = max(diagrams, key=diagram_version)
    else:
        scope = root
    geoms = {}
    raw_labels = {}
    for cell in scope.iter("mxCell"):
        cid = cell.get("id")
        if not cid:
            continue
        geom = cell.find("mxGeometry")
        x = float(geom.get("x", 0)) if geom is not None else 0.0
        y = float(geom.get("y", 0)) if geom is not None else 0.0
        geoms[cid] = (x, y, cell.get("parent"))
        value = cell.get("value")
        if value:
            raw_labels[cid] = value

    cache = {}
    cells = []
    for cid, raw in raw_labels.items():
        label = _clean_drawio_label(raw)
        # 도형 라벨에 원본 XML 태그/URL-인코딩 흔적이 섞인 손상된 값은(작성자가 실수로
        # 다른 다이어그램을 붙여넣은 경우 등, 실측 확인) 건너뛴다 — 지어내는 것보다 낫다.
        if not label or "mxgraphmodel" in label.lower() or "%3c" in label.lower():
            continue
        x, y = _abs_pos(cid, geoms, cache)
        cells.append((y, x, label))
    cells.sort(key=lambda c: (c[0], c[1]))
    return "\n".join(f"- {label}" for _, _, label in cells)


_XLSX_NS = "{http://schemas.openxmlformats.org/spreadsheetml/2006/main}"
_XLSX_PKG_REL_NS = "{http://schemas.openxmlformats.org/package/2006/relationships}"
_XLSX_DOC_REL_NS = "{http://schemas.openxmlformats.org/officeDocument/2006/relationships}"
# 표가 큰 시트(예: Install 719행)를 통째로 넣으면 컨텍스트가 터지므로 시트당/전체
# 상한을 둔다. Cover/Install처럼 실제 데이터 시트를 우선하고 Pivot_*/Lists 같은
# 내부용 hidden 시트는 처음부터 건너뛴다.
_XLSX_MAX_ROWS_PER_SHEET = 300
_XLSX_MAX_TOTAL_CHARS = 15000


def _xlsx_shared_strings(z):
    try:
        data = z.read("xl/sharedStrings.xml")
    except KeyError:
        return []
    root = ET.fromstring(data)
    return ["".join(t.text or "" for t in si.iter(f"{_XLSX_NS}t")) for si in root.findall(f"{_XLSX_NS}si")]


def _xlsx_sheet_paths(z):
    """workbook.xml(숨김 제외 시트 이름+r:id) + workbook.xml.rels(r:id -> 실제 경로)를
    엮어서 [(시트이름, zip내경로), ...]를 문서 순서대로 반환한다."""
    wb = ET.fromstring(z.read("xl/workbook.xml"))
    sheets = [
        (sh.get("name"), sh.get(f"{_XLSX_DOC_REL_NS}id"))
        for sh in wb.find(f"{_XLSX_NS}sheets")
        if sh.get("state") != "hidden"
    ]
    rels = ET.fromstring(z.read("xl/_rels/workbook.xml.rels"))
    rel_map = {r.get("Id"): r.get("Target") for r in rels.findall(f"{_XLSX_PKG_REL_NS}Relationship")}
    result = []
    for name, rid in sheets:
        target = rel_map.get(rid)
        if not target:
            continue
        result.append((name, target if target.startswith("xl/") else f"xl/{target}"))
    return result


def _xlsx_cell_value(c, shared):
    t = c.get("t")
    if t == "inlineStr":
        is_el = c.find(f"{_XLSX_NS}is")
        return "".join(tt.text or "" for tt in is_el.iter(f"{_XLSX_NS}t")) if is_el is not None else ""
    v = c.find(f"{_XLSX_NS}v")
    if v is None or v.text is None:
        return ""
    if t == "s":
        idx = int(v.text)
        return shared[idx] if 0 <= idx < len(shared) else ""
    return v.text


def _parse_xlsx_sheet_rows(xml_bytes, shared, max_rows):
    root = ET.fromstring(xml_bytes)
    sheet_data = root.find(f"{_XLSX_NS}sheetData")
    if sheet_data is None:
        return []
    lines = []
    for row in list(sheet_data)[:max_rows]:
        values = [v for v in (_xlsx_cell_value(c, shared) for c in row) if v not in ("", None)]
        if values:
            lines.append(" | ".join(values))
    return lines


_XLSX_SKIP_SHEET_RE = re.compile(r'과거|pivot|^lists$', re.IGNORECASE)


def _parse_xlsx_text(xlsx_bytes):
    """xlsx는 zip 컨테이너 안에 시트별 XML이 들어있는 구조라(실측 확인) openpyxl/pandas
    없이 표준 라이브러리(zipfile + xml.etree)만으로 파싱 가능 — 이 환경은 pip 자체가
    깨져있어(pyOpenSSL 버전 충돌) 새 패키지 설치가 안 되므로 의도적으로 무의존성 유지.
    "Install" 류 이름의 시트를 최우선으로 두고(실측: Cover의 revision history가 먼저
    나오면 그것만으로 상한을 거의 다 써버려 정작 필요한 데이터 시트가 밀림), 과거
    스냅샷/피벗용 내부 시트는 건너뛴 뒤, 전체 글자수 상한(_XLSX_MAX_TOTAL_CHARS)에서 멈춘다."""
    out = []
    total = 0
    with zipfile.ZipFile(io.BytesIO(xlsx_bytes)) as z:
        shared = _xlsx_shared_strings(z)
        sheets = _xlsx_sheet_paths(z)
        order = {path: i for i, (_, path) in enumerate(sheets)}
        sheets.sort(key=lambda s: (0 if "install" in s[0].lower() else 1, order[s[1]]))
        for name, path in sheets:
            if _XLSX_SKIP_SHEET_RE.search(name or ""):
                continue
            if total >= _XLSX_MAX_TOTAL_CHARS:
                break
            try:
                xml_bytes = z.read(path)
            except KeyError:
                continue
            rows = _parse_xlsx_sheet_rows(xml_bytes, shared, _XLSX_MAX_ROWS_PER_SHEET)
            if not rows:
                continue
            block = f"## Sheet: {name}\n" + "\n".join(rows)
            budget_left = _XLSX_MAX_TOTAL_CHARS - total
            if len(block) > budget_left:
                block = block[:budget_left] + "\n(글자수 상한 도달로 이하 생략)"
            out.append(block)
            total += len(block)
    return "\n\n".join(out) if out else None


file_store.XLSX_PARSER = _parse_xlsx_text  # 업로드된 xlsx도 같은 무의존성 파서로 읽는다


def _confluence_rest_headers():
    if not _CONF_ENV:
        return None, None
    base = _CONF_ENV["ATLASSIAN_BASE_URL"].rstrip("/")
    auth = base64.b64encode(
        f"{_CONF_ENV['ATLASSIAN_EMAIL']}:{_CONF_ENV['ATLASSIAN_API_TOKEN']}".encode()
    ).decode()
    return base, {"Authorization": f"Basic {auth}", "Accept": "application/json"}


def _download_latest_attachment(page_id, suffix, timeout=15):
    """페이지에 직접 첨부된 파일 중 suffix(예: ".drawio", ".xlsx")로 끝나는 것 중
    파일명의 "v1.06"/"V1.69" 같은 버전 표기가 가장 높은 걸 골라 (제목, bytes)로
    받아온다. 버전 표기가 없으면 순서상 마지막 것을 쓴다. 첨부 자체가 없으면 (None, None)."""
    base, headers = _confluence_rest_headers()
    if not headers:
        return None, None
    atts = _get_attachments(page_id, headers, base, {}, timeout)
    matches = [a for a in atts if a.get("title", "").lower().endswith(suffix)]
    if not matches:
        return None, None

    def version_key(a):
        m = _DRAWIO_VERSION_RE.search(a.get("title", ""))
        return (int(m.group(1)), int(m.group(2))) if m else (0, 0)

    matches.sort(key=version_key, reverse=True)
    target = matches[0]
    dl_path = target["_links"]["download"]
    dl_url = f"{base}{dl_path}" if dl_path.startswith("/wiki") else f"{base}/wiki{dl_path}"
    dl_headers = {k: v for k, v in headers.items() if k.lower() != "accept"}
    req = urllib.request.Request(dl_url, headers=dl_headers)
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return target.get("title"), resp.read()


def _fetch_drawio_text(page_id, timeout=15):
    """페이지에 직접 첨부된 .drawio 파일 중 가장 높은 버전 하나를 받아 텍스트로
    파싱한다. Rovo Search가 본문 없는 페이지를 찾아와도(atlassian_mcp_client._EMPTY_BODY_NOTE)
    다이어그램 자체엔 실제 시퀀스 내용이 있는 경우가 흔해서(실측: "06_EMS+ MCU-MPU
    Initialization sequence") 이걸로 보완한다. 실패/첨부없음 시 None."""
    try:
        title, data = _download_latest_attachment(page_id, ".drawio", timeout)
        if not data:
            return None
        return _parse_drawio_sequence(data)
    except Exception as e:
        print(f"⚠️  drawio 다운로드/파싱 실패(page={page_id}): {e}", file=sys.stderr)
        return None


def _fetch_xlsx_text(page_id, timeout=20):
    """페이지에 직접 첨부된 .xlsx 파일 중 가장 높은 버전 하나를 받아 시트별 텍스트
    테이블로 파싱한다(실측: "03_Install Document (MPU-MCU)"처럼 본문 없이 Install Group
    정의를 엑셀로만 관리하는 페이지가 있음). 실패/첨부없음 시 None."""
    try:
        title, data = _download_latest_attachment(page_id, ".xlsx", timeout)
        if not data:
            return None
        return _parse_xlsx_text(data)
    except Exception as e:
        print(f"⚠️  xlsx 다운로드/파싱 실패(page={page_id}): {e}", file=sys.stderr)
        return None


def _fetch_attachment_text(page_id, timeout=20):
    """본문이 빈 페이지를 보완할 첨부파일 텍스트를 찾는다 — .drawio, .xlsx 순으로
    시도해서 처음 찾은 것 하나를 쓴다(한 페이지에 둘 다 있는 경우는 아직 못 봤음,
    있다면 이후 필요에 따라 둘 다 합치도록 확장)."""
    text = _fetch_drawio_text(page_id, timeout)
    if text:
        return text, "drawio 다이어그램"
    text = _fetch_xlsx_text(page_id, timeout)
    if text:
        return text, "엑셀 첨부파일"
    return None, None


def _fetch_live_images(soup, page_id, headers, base, att_cache, timeout):
    """본문 텍스트 추출로는 안 잡히는 실제 이미지(설계/화면 문서에 흔함)를 라이브로
    받아온다. 두 가지 임베드 방식을 모두 처리:
    - drawio 매크로: 렌더링된 PNG가 매크로의 pageId 파라미터가 가리키는 "다른" 페이지에
      "{diagramDisplayName}.png"라는 이름의 첨부파일로 존재 (confluence_fetch_diagrams.py와 동일 로직)
    - ac:image(첨부 스크린샷 등): 같은 페이지의 첨부파일을 ri:attachment/@ri:filename으로 직접 참조
    반환: [(표시용 이름, 로컬 파일명), ...]
    """
    found = []

    for macro in soup.find_all("ac:structured-macro", attrs={"ac:name": ["drawio", "drawio-sketch"]}):
        params = {p.get("ac:name"): p.get_text() for p in macro.find_all("ac:parameter", recursive=False)}
        diagram_name = params.get("diagramDisplayName") or params.get("diagramName")
        src_page_id = params.get("pageId")
        if not diagram_name or not src_page_id:
            continue
        fname = f"{src_page_id}_{_slugify(diagram_name)}.png"
        dest = LIVE_DIAGRAM_DIR / fname
        if not dest.exists():
            atts = _get_attachments(src_page_id, headers, base, att_cache, timeout)
            png_title = diagram_name + ".png"
            match = (next((a for a in atts if a.get("title") == png_title), None)
                     or next((a for a in atts if a.get("title", "").endswith(png_title)
                              and a.get("extensions", {}).get("mediaType") == "image/png"), None)
                     or next((a for a in atts if a.get("title", "").startswith(diagram_name)
                              and a.get("extensions", {}).get("mediaType") == "image/png"), None))
            if not match:
                continue
            try:
                _download_attachment(match, headers, base, dest, timeout)
            except Exception as e:
                print(f"⚠️  다이어그램 다운로드 실패 {diagram_name}: {e}", file=sys.stderr)
                continue
        found.append((diagram_name, fname))

    for img in soup.find_all("ac:image"):
        att_ref = img.find("ri:attachment")
        if not att_ref:
            continue
        filename = att_ref.get("ri:filename")
        if not filename:
            continue
        fname = f"{page_id}_{_slugify(filename)}"
        dest = LIVE_DIAGRAM_DIR / fname
        if not dest.exists():
            atts = _get_attachments(page_id, headers, base, att_cache, timeout)
            match = next((a for a in atts if a.get("title") == filename), None)
            if not match:
                continue
            try:
                _download_attachment(match, headers, base, dest, timeout)
            except Exception as e:
                print(f"⚠️  이미지 다운로드 실패 {filename}: {e}", file=sys.stderr)
                continue
        found.append((filename, fname))

    return found


def _page_to_result(item, base, terms, require_body_match, headers, att_cache, timeout):
    title = item.get("title", "(제목 없음)")
    page_id = item.get("id", "")
    storage = item.get("body", {}).get("storage", {}).get("value", "")
    if not storage:
        return None
    soup = BeautifulSoup(storage, "html.parser")
    text = render_storage_html(soup)

    lower = text.lower()
    match_pos = -1
    for t in terms:
        p = lower.find(t.lower())
        if p >= 0:
            match_pos = p
            break
    # require_body_match=True인 경로(본문 fuzzy 검색)는 CQL text~가 관계없는
    # 페이지까지 fuzzy/stemmed 매치시키는 경우가 흔해서 본문에 실제로 있는지 재검증한다.
    # 제목 검색으로 찾은 경우는 이미 제목 자체가 신뢰할 근거라, 본문이 다이어그램
    # 위주(스크린 디자인 문서 등)라 텍스트가 거의 없어도 그대로 채택한다.
    if require_body_match and terms and match_pos < 0:
        return None

    # 매치 위치 주변으로 발췌 (naive head truncation은 실제 관련 내용을 놓칠 수 있음)
    if match_pos >= 0:
        start = max(0, match_pos - 800)
        excerpt = text[start:start + 3000]
    else:
        excerpt = text[:3000]

    try:
        images = _fetch_live_images(soup, page_id, headers, base, att_cache, timeout)
    except Exception as e:
        print(f"⚠️  라이브 이미지 처리 실패: {e}", file=sys.stderr)
        images = []
    if images:
        img_md = "\n".join(f"![{name}](confluence-images/{fname})" for name, fname in images)
        excerpt = img_md + "\n\n" + excerpt

    page_url = f"{base}/wiki/pages/viewpage.action?pageId={page_id}" if page_id else ""
    return {"title": title, "url": page_url, "text": excerpt}


def search_confluence_live(query, limit=3, timeout=8):
    """Confluence CQL 검색으로 관련 페이지 body를 라이브로 가져와 텍스트 변환.

    text~ 검색만 쓰면(예전 방식) "TOU" 같은 짧은 기술 용어에서 Confluence의
    fuzzy/stemmed 매치가 완전히 무관한 페이지(주간 업무 보고서, 무관한 회의록 등)를
    돌려주고, 정작 제목에 "TOU"가 명시된 실제 설계 문서(스크린 디자인처럼 본문이
    다이어그램 위주라 텍스트가 적은 문서 포함)는 상위에 아예 안 나오는 경우가 실측됨.
    그래서 title~ 검색을 우선 시도하고, 제목에 매치가 없을 때만 text~로 폴백한다.
    """
    if not _CONF_ENV:
        return []

    base = _CONF_ENV["ATLASSIAN_BASE_URL"].rstrip("/")
    auth = base64.b64encode(
        f"{_CONF_ENV['ATLASSIAN_EMAIL']}:{_CONF_ENV['ATLASSIAN_API_TOKEN']}".encode()
    ).decode()
    headers = {"Authorization": f"Basic {auth}", "Accept": "application/json"}

    # CQL 검색은 순수 자연어 질문(조사·물음표 포함)엔 매치가 잘 안 되므로
    # wiki_ask.py와 동일한 방식으로 기술 용어/조사 제거된 형태로 정제한다
    search_term = _tech_query(query)
    if search_term == query:  # 기술 용어가 없어 원문 그대로 반환된 경우
        search_term = _clean_query(query)
    safe_query = search_term.replace('"', ' ').strip()
    if not safe_query:
        return []
    terms = [t for t in safe_query.split() if t]

    title_cql = f'{_CQL_SPACE_CLAUSE} and type = page and title ~ "{safe_query}"'
    try:
        data = _cql_search(base, headers, title_cql, limit, timeout)
    except Exception as e:
        print(f"⚠️  Confluence 라이브 검색 실패(title): {e}", file=sys.stderr)
        data = {"results": []}

    require_body_match = False
    if not data.get("results"):
        text_cql = f'{_CQL_SPACE_CLAUSE} and type = page and text ~ "{safe_query}"'
        try:
            data = _cql_search(base, headers, text_cql, limit, timeout)
        except Exception as e:
            print(f"⚠️  Confluence 라이브 검색 실패(text): {e}", file=sys.stderr)
            return []
        require_body_match = True

    att_cache = {}
    results = []
    for item in data.get("results", []):
        r = _page_to_result(item, base, terms, require_body_match, headers, att_cache, timeout)
        if r:
            results.append(r)
    return results


_CONTENT_CHARS = re.compile(r'[가-힣A-Za-z0-9]+')


def _residual_content_length(text):
    """매체 요청/질문어 등 노이즈 문구를 제거하고 실제로 남는 내용어 길이를 잰다.
    _clean_query는 이 결과가 너무 짧으면 원문을 그대로 반환하는 자체 폴백이 있어서
    "정제해도 남는 게 없다"는 신호로 못 쓴다 — 여기서는 그 폴백 없이 직접 측정."""
    stripped = _KO_STOP.sub(' ', text)
    return sum(len(t) for t in _CONTENT_CHARS.findall(stripped))


def _build_search_query(history):
    """대화의 마지막 메시지만 검색어로 쓰면 "그림으로 보여줘"처럼 그 자체로는
    주제가 없는 후속 질문에서 완전히 엉뚱한 문서가 검색된다. 정제 후 내용이 거의
    안 남으면 직전 사용자 발화까지 합쳐서 검색 문맥을 보강한다."""
    user_turns = [h["content"] for h in history if h.get("role") == "user"]
    last = user_turns[-1] if user_turns else ""
    if len(user_turns) < 2:
        return last
    if _residual_content_length(last) <= 3:
        return f"{user_turns[-2]} {last}"
    return last


# 문법상으로만 살아남는 접속/의문 단어들. 원래는 _extract_terms의 정규식 추출
# 결과에서 "로직"/"커미셔닝" 같은 내용어를 위해 한글 2글자+를 다 뽑다 보니 같이
# 딸려오는 노이즈("TOU 관련해서 로직에 대해 설명한 페이지 있니?" -> "관련/대해/
# 설명한/있니")를 거르는 용도였다.
#
# 2026-09-17부터 역할이 바뀌었다: build_context()의 1차(원본) 검색어는 이제
# _expand_search_query()의 LLM "core" 필드가 만든다(문법적 잡음을 일반적으로
# 제거 — 새 잡음 패턴이 나올 때마다 여기 단어를 추가하는 방식에서 탈피하려는
# 목적, 사용자 지적: "이거 하나하나 룰베이스로 하면 끝도 없어"). 이 집합은 이제
# 그 LLM 정제를 대체하지 않고, **이미 실제 사고를 낸 적 있는 단어에 한해서만**
# LLM이 어쩌다 놓쳤을 때의 값싼 최후 안전망 역할만 한다(claude -p 샘플링
# 비결정성 — 같은 질문도 실행마다 core 정제 정도가 살짝 다를 수 있음, 실측
# 확인). 새 단어를 습관적으로 추가하지 말고, 반복 재현되는 실패만 여기 담을 것
# — 일반적인 케이스는 query_expansion.py의 <instructions>를 고쳐서 대응해야 한다.
_GENERIC_KO_WORDS = {
    "관련", "대해", "대한", "있니", "있나요", "있어", "있음", "무엇",
    "어떤", "설명", "설명한", "설명해", "부분", "내용", "관해서", "관해",
    # 인물 이름에 붙는 범용 호칭 접미사(2026-09-17 추가). "장승혁이 누구야"처럼
    # 사용자 질문에 "프로/님/씨"가 그대로 들어있으면 원본 검색어에 섞여 들어가는데,
    # 이 회사 문서(특히 주간업무 보고서)는 거의 모든 사람 이름 뒤에 "프로"를 습관적으로
    # 붙여서 이 단어 자체는 특정 인물을 전혀 구분해주지 못한다. 오히려 "프로"가 수십 번
    # 반복되는 다른 사람들 명단 문서(예: "2026 CW38 Energy SW part 주간 업무")가 Rovo
    # 관련도 상위로 올라오고 진짜 그 인물을 언급한 문서는 밀려나는 사고가 실측됨
    # (rovo_search("장승혁 프로") 상위 5개 전부 무관한 주간보고 vs rovo_search("장승혁")
    # 상위 5개는 실제로 그가 등장하는 특허/자산/휴가 문서 — "gem net id"류 도메인
    # 이탈과는 다른, "이름을 흐리는 초고빈도 부가어" 축의 새로운 실패 패턴).
    "프로", "님", "씨",
    # "누구야"류 의문사(2026-09-17 추가). `_KO_STOP`(search_query_utils.py)은 "뭐야"는
    # 걸러내지만 "누구야"는 안 걸러내서, "장승혁이 누구야" 같은 흔한 인물 질문에서
    # "누구야"가 그대로 검색어에 남는다. 4글자라 person_name_candidates(한글 2~4자
    # 정규식)에도 false-positive로 걸려서 "누구야"를 인물 이름으로 오인해 작성자 검색
    # 앵커 후보로 취급할 뻔한 것까지 실측으로 발견 — 이름 후보 오염 방지 차원에서도 필요.
    "누구야", "누구", "누군지", "누구인지",
}

# 인물 문서 "개수/전체 목록" 질문 판별(2026-09-18 추가) — "장승혁이 몇 개 썼어",
# "정지석이 만든 문서 다 보여줘" 같은 질문은 build_context의 anchor 보강 경로를
# 타되, 일반 인물 질문과 다르게 처리해야 한다: 답이 "예시 문서 몇 개"가 아니라
# "정확한 개수/전체 목록" 자체이므로, 아래에서 이 정규식에 걸리면 search_by_creator
# 의 6건 상한(예시용)을 풀고 전체를 컨텍스트에 넣는다. 사람 이름을 여기 하드코딩하지
# 않는다 — anchor_author_id가 이미 find_author_id_by_title()/캐시로 풀린 뒤에만
# 이 분기를 타므로, 이 정규식은 "누구를" 찾는 게 아니라 "무엇을 답해야 하는지"만
# 판별한다.
_PERSON_DOC_COUNT_RE = re.compile(r"몇\s*(개|건|페이지|문서)|얼마나\s*(많이|많은)")
_PERSON_DOC_LISTALL_RE = re.compile(r"(다|전부|모두)\s*(찾아|보여|알려|줘)")

# 인물 "Jira 티켓" 질문 판별(2026-09-18 추가, 사용자 요청: "Jira 티켓도
# 연동할수있어? 심철로 할당된 jira 티켓 찾아줘"). "jira"/"지라"라는 단어 자체가
# 이미 충분히 구체적인 고유명사라(흔한 한글 조사/명사와 겹칠 일이 없음) "티켓"/
# "이슈"와 함께 나오면 오탐 위험 없이 바로 의도를 판별할 수 있다 — 위 개수/목록
# 정규식과 달리 사람 이름을 오인할 여지 있는 일반 명사가 아니므로 정규식으로도
# 안전하다.
_JIRA_TICKET_RE = re.compile(r"(jira|지라)", re.IGNORECASE)
# 팀 전체 Jira 집계 판별(2026-10-07 추가) — "EnergySW 파트에서 jira 티켓이 가장 많이 할당된 사람은?
# assign" 질문이 팀 명단 집계 분기로 갔는데, 그 분기는 Confluence 작성 문서 수만 세서 "Jira 할당
# 집계가 없다"고 답하고 출처에도 Jira가 없었다(사용자 지적: "jira 연동하기로 했잖아"). 인물 한
# 명용 _JIRA_TICKET_RE 판별과 같은 원칙으로, jira/지라 + 티켓·이슈·할당·assign이 같이 나오면
# Jira 할당 수 집계로 보낸다.
_JIRA_ASSIGN_WORDS_RE = re.compile(r"(티켓|이슈|할당|assign)", re.IGNORECASE)
_JIRA_TICKET_WORD_RE = re.compile(r"(티켓|ticket)", re.IGNORECASE)
# "가장 많이 할당된 사람"처럼 팀 내 순위를 묻는 표현 — expanded_wants_stats(LLM 판단)가 가끔 놓쳐도
# Jira 팀 집계로 보내기 위한 값싼 안전망(Jira 할당 질문과 함께 나올 때만 쓰인다).
_TEAM_RANK_RE = re.compile(r"(가장|제일|누가|순위|랭킹|top|많이)", re.IGNORECASE)


def _jira_assigned_jql_url(account_id):
    """그 사람에게 할당된 진행 중 이슈 목록을 Jira에서 바로 여는 링크(출처 카드용)."""
    jql = f'assignee = "{account_id}" AND statusCategory != Done AND status != Cancelled ORDER BY priority DESC'
    return f"https://{ATLASSIAN_SITE}/issues/?jql=" + urllib.parse.quote(jql)


def _rovo_search_query(query, extra_terms=None):
    """Rovo Search는 짧은 키워드 질의에서 훨씬 정확함(실측: "Advanced TOU 로직"은
    정확 매치, 자연어 문장 그대로는 노이즈↑). 한글 내용어까지 포함해서 뽑되
    (_extract_terms), 의미 없는 접속/의문 단어는 걸러낸다.

    extra_terms로 원본 질문의 키워드를 함께 넘기면 뒤에 이어붙인다(중복 제거) —
    _expand_search_query가 도메인을 잘못 짚어 엉뚱한 동의어로 확장했을 때(실측:
    "gem net id ffff"를 UL1741SB/CSIP/IEEE2030.5 같은 그리드 연계 표준 쪽으로 확장 —
    net id를 GEM-MI PLC 통신이 아니라 전력망 "네트워크"로 오인), 원본에 있던
    "gem"/"netid"/"ffff" 같은 축약어가 검색어에서 완전히 사라지는 것을 막기 위함."""
    terms = [t for t in _extract_terms(query) if t not in _GENERIC_KO_WORDS]
    if extra_terms:
        seen = {t.lower() for t in terms}
        for t in extra_terms:
            if t.lower() not in seen:
                terms.append(t)
                seen.add(t.lower())
    return " ".join(terms) if terms else query


# 검색어 확장(claude -p 프롬프트 엔지니어링)은 query_expansion.py로 분리됨 —
# _expand_search_query/_expand_search_query_llm_call 정의는 그쪽 참고.


# Rovo Chat(Atlassian 네이티브)의 검색→재검색 반복 에이전트 동작을 얕게 모방하는 재시도
# 루프를 2026-09-11에 추가했다가 같은 날 되돌렸다: claude -p 서브프로세스가 하나 더 늘어
# 응답이 10~15초 느려지는데, LLM의 "충분한지" 판단이 호출마다 달라져 결과가 오히려
# 들쭉날쭉해지는 게 실측됨(같은 질문인데 어떤 실행은 소스 7개, 어떤 실행은 4개). 득보다
# 실이 커서 원래의 "원본+확장 2회 검색"으로 되돌림 — 결과가 빈약한 케이스는 재시도 루프
# 대신 [[project_wikibot_architecture]]에 기록된 다른 방식(개인 스페이스 라벨링 등)으로 보완.
def build_context(question, history=None, tracer=None):
    tr = tracer or Tracer()  # 작업대 단계 표시용(호출부가 안 넘기면 기록만 하고 버린다)
    # 로컬 위키(docs/*.md, "1.1 ~ 24.6" 번호 체계)는 사용자 확정으로 답변 소스에서
    # 완전히 제외 — 라이브 Confluence 페이지만 근거로 쓴다(실측: 로컬 위키 청크가
    # 질문과 느슨하게만 연관된 범용 아키텍처 문서를 끌어와 답변이 부정확해짐).
    #
    # Rovo/Confluence 검색은 원문 그대로면 "동작원리"처럼 문서 제목과 다른 표현을
    # 못 찾는 경우가 있어(실측: "architecture" 확장 시에만 관련 문서 1순위) 확장된
    # 질의를 쓴다. Rovo Search(Atlassian 공식 검색엔진, claude mcp login atlassian으로
    # 받아둔 OAuth 토큰 재사용, 무료)가 우리가 직접 짠 CQL 검색보다 훨씬 정확함(실측:
    # "Advanced TOU 로직" 질의에서 원하는 문서 1·2순위 정확 매치 + Jira까지 덤으로).
    # MCP 토큰이 없거나 네트워크 문제로 실패하면 빈 리스트가 오므로 기존 CQL 검색으로
    # 폴백한다. limit=5는 한때 getConfluencePage 전체 본문 fetch와 합쳐지면 표 하나가
    # 주간업무 노이즈 여러 개 사이에 묻혀서 작은 모델이 못 찾는 문제가 실측되어 3으로
    # 축소했었으나, "gem net id" 케이스(질문 확장이 엉뚱한 방향으로 튀면서 정답 문서가
    # 3위 밖으로 밀려 아예 안 잡힘)가 실측되어 사용자 확정으로 다시 5로 되돌림
    # (2026-08-19) — 표-노이즈 리스크가 재발하면 그때 다시 조정.
    # 검색용으로만 인물 한글 이름 -> 영문 표기를 보강(원문 question 자체는 바꾸지 않음 —
    # 이 함수는 build_context 로컬 변수만 다루고, 대화 이력/최종 답변 프롬프트에 쓰이는
    # 원문은 history 쪽에 그대로 남아있다).
    question = _apply_person_aliases(question)
    # 원본(1차) 검색어 정제는 더 이상 정규식 불용어 목록으로 하지 않는다(2026-09-17,
    # 사용자 지적: "이거 하나하나 룰베이스로 하면 끝도 없어, 좋은 방법 없니?" — 프로/
    # 님/씨/누구야/"/" 등을 정규식으로 하나씩 걸러내다 보니 새 케이스가 나올 때마다
    # 또 패치해야 했음). 대신 이미 쓰고 있던 검색어 확장 LLM 호출(claude -p, 1회만
    # 호출 — 지연 추가 없음)이 "core"(문법적 잡음만 제거한 신뢰 가능한 원본 검색어)와
    # "keywords"(동의어/표기 변형 포함 보완 검색어)를 한 번에 뽑게 구조를 바꿨다
    # (query_expansion.py의 _INSTRUCTIONS/_FEWSHOT_EXAMPLES 참고). claude -p 실패 시
    # (question, question, None)으로 폴백하므로 아래 로직은 항상 안전하게 동작한다.
    # 세 번째 값 expanded_person은 질문이 특정 인물 한 명을 콕 집어 물을 때만
    # 그 이름이 채워진다 — 아래 anchor 보강 분기(person_name_candidates 정규식이
    # 있던 자리)에서 쓴다. 네 번째 값 expanded_wants_stats는 "누가 가장 많이
    # 썼어"류 개수/순위/전체목록 질문 판별을 정규식 대신 이 LLM 판단에도 같이
    # 맡긴 것(2026-09-22, 아래 _PERSON_DOC_COUNT_RE 근처 설명 참고).
    with tr.step("expand", "질문 분석 · 검색어 확장") as st:
        original_query, expanded_keywords, expanded_person, expanded_wants_stats = _expand_search_query(question, history)
        st.items = [f"확장 키워드: {expanded_keywords}"]
        if expanded_person:
            st.items.append(f"지목된 인물: {expanded_person}")
        if expanded_wants_stats:
            st.items.append("개수·순위·목록 질문으로 판단")
        st.detail = "핵심 검색어 “{}”".format(
            " ".join(t for t in original_query.split() if t not in _GENERIC_KO_WORDS) or original_query
        )
    # claude -p는 같은 지시에도 매번 똑같이 순종하지 않는다(샘플링 비결정성, 위
    # _expand_search_query_llm_call 주석 참고) — 실측: "장승혁 프로가 누구야"를 여러 번
    # 물으면 대부분 core="장승혁"으로 깨끗하게 나오지만, 가끔 "장승혁 프로가 누구야"를
    # 거의 그대로 core에 남기는 경우가 나옴(3회 연속 성공 후 다른 실행에서 1회 재현).
    # 이 최소 집합(_GENERIC_KO_WORDS)은 LLM 정제 규칙을 대체하는 게 아니라, 이미
    # 실제로 사고를 낸 적 있는 단어들만 담은 값싼 안전망이다 — 카테고리를 넓혀가며
    # 계속 키우는 게 아니라, LLM이 어쩌다 놓쳤을 때 최후 방어선 역할만 한다.
    original_query = " ".join(
        t for t in original_query.split() if t not in _GENERIC_KO_WORDS
    ) or original_query
    original_terms = original_query.split()

    # 원본 질의를 먼저 검색해 최우선 후보로 삼고, 확장 질의 결과로 보완한다(원본 우선 +
    # 확장 보완 병합) — "extra_terms로 확장 질의 문자열 뒤에 원본 키워드를 붙이는" 이전
    # 방식은 부족했다. 실측: "gem net id ffff" 질문에서 claude -p 확장이 "GEM"을 매번
    # 다른 업계 표준(SECS/GEM 반도체 설비 통신, GPON GEM 광통신 포트 등 우리 도메인과
    # 무관한 것)으로 오인해 그쪽 용어를 검색어에 잔뜩 섞어 넣었고, 원본 키워드를 뒤에
    # 이어붙이는 것만으로는 그 노이즈를 못 이겨서 4번 중 1번꼴로 정답 문서가 아예
    # 후보에서 빠지는 게 재현됨. 반면 원본 질의("gem net id ffff") 단독 검색은 4번
    # 전부 정답 문서를 Rovo 1위로 정확히 찾음 — 그래서 원본 질의를 신뢰의 기준으로 삼고,
    # 확장 질의는 "동작원리→architecture"류(실측 검증된 이득)의 동의어 보완 용도로만
    # 추가한다(2026-08-19, "gem net id" 케이스로 재현/수정).
    # Jira 할당 질문이면 Rovo 문서 검색을 건너뛰고 바로 Jira를 조회한다(2026-10-07, 사용자 지적:
    # "작업 단계에서도 rovo가 아닌 jira를 탐색해야되는거 아냐?"). 할당 수/목록은 Rovo 본문 검색으로는
    # 나오지 않는 정보라, Rovo 2회(약 15초)는 시간만 쓰고 무관한 Confluence 문서 수십 건을 참고 자료에
    # 섞었다(실측: 참고 자료 24건 · 3.5만 자). 인물 한 명(아래 anchor 분기의 wants_jira_tickets)과 팀
    # 전체(아래 명단 분기의 wants_team_jira) 둘 다 해당. 계정을 못 찾는 등으로 Jira 결과가 비면 맨
    # 아래 CQL 폴백이 그대로 돈다.
    # "티켓"은 이 조직에선 곧 Jira 티켓이라 단독으로도 Jira 질문으로 본다(실측: "심철로된 티켓 알려줘.
    # 우선순위별"에 jira라는 단어가 없어 Confluence 작성 문서로 답함). 이슈·할당·assign은 일반 단어로도
    # 쓰이므로 jira/지라와 같이 나올 때만 인정한다.
    jira_assign_intent = bool(_JIRA_TICKET_WORD_RE.search(question)) or (
        bool(_JIRA_TICKET_RE.search(question)) and bool(_JIRA_ASSIGN_WORDS_RE.search(question))
    )
    team_stats_intent = bool(
        expanded_wants_stats or _PERSON_DOC_COUNT_RE.search(question) or _PERSON_DOC_LISTALL_RE.search(question)
        # 순위 표현은 Jira 할당 질문일 때만 추가로 인정(일반 질문의 "누가"까지 3분짜리 명단 집계로 보내지 않게)
        or (jira_assign_intent and _TEAM_RANK_RE.search(question))
    )
    jira_direct = jira_assign_intent and (bool(expanded_person) or team_stats_intent)
    if jira_direct:
        live_pages = []
        tr.note("search", "Rovo 검색 (Confluence·Jira 본문)", "Jira 할당 질문이라 문서 검색은 건너뛰고 Jira를 직접 조회합니다", status="skip")
    else:
        with tr.step("search", "Rovo 검색 · 원본 질의") as st:
            live_pages = rovo_search(original_query, limit=3)
            st.detail = f"{len(live_pages)}건"
            st.items = [p["title"] for p in live_pages]
    using_rovo = bool(live_pages)
    for p in live_pages:
        p["source"] = "rovo"

    if jira_direct:
        pass
    elif using_rovo and expanded_keywords.strip() != original_query.strip():
        with tr.step("search_expanded", "Rovo 검색 · 확장 질의") as st:
            expanded_pages = rovo_search(
                _rovo_search_query(expanded_keywords, extra_terms=original_terms), limit=3, two_hop=False
            )
            seen_urls = {p["url"] for p in live_pages}
            added_titles = []
            for p in expanded_pages:
                if p["url"] not in seen_urls:
                    p["source"] = "rovo"
                    live_pages.append(p)
                    seen_urls.add(p["url"])
                    added_titles.append(p["title"])
            st.detail = f"{len(expanded_pages)}건 중 새 문서 {len(added_titles)}건"
            st.items = added_titles
    else:
        tr.note("search_expanded", "Rovo 검색 · 확장 질의", "원본 질의와 같아 건너뜀" if using_rovo else "Rovo 응답 없음 — CQL 폴백 예정", status="skip")

    # 인물 질문 보강: 질문에 한글 이름이 있으면 그 이름이 제목에 들어간 Confluence
    # 문서를 CQL title ~ 검색으로 직접, 결정적으로 찾아 작성자 계정(author_id)을
    # 얻고, 그 계정으로 Confluence를 통째로 재검색해서 원본/확장 텍스트 검색이
    # 놓친 문서까지 보강한다. 사용자가 직접 지적(2026-09-17): "confluence에서
    # 작성자 이름에서 못찾니?" — Rovo Search는 본문 텍스트 매칭 위주라 예산
    # 품의서/회의록처럼 이름이 스치듯 한두 번만 나오는(작성자 본인은 자기 이름을
    # 문서 안에 잘 안 씀) 문서를 놓치는데, 실제로 Confluence 메타데이터로 "장승혁"
    # 계정을 찾아 재검색하니 FCAS 정리/JWG 미팅록/연구소 소개 문서 등 텍스트
    # 검색으론 전혀 안 걸리던 문서가 대거 나옴(실측 확인).
    #
    # 처음엔 rovo_search 결과(live_pages) 중 제목에 이름이 들어간 문서를 찾는
    # 방식이었는데, "jack jang/장승혁 프로가 누구야?"처럼 흔한 영단어("jack"/
    # "jang")가 검색어에 섞이면 Rovo의 불투명한 랭킹이 "(장승혁)" 문서 자체를
    # 상위 결과에서 아예 빼버려서 anchor를 못 찾는 재발이 실측됨(사용자가 3번
    # 연속 재현시킴, 2026-09-17). find_author_id_by_title()은 CQL title ~ 연산자로
    # Rovo 랭킹을 거치지 않고 직접 찾으므로 이 문제에서 자유롭다.
    #
    # 후보를 core(원본 질의)뿐 아니라 expanded_keywords(확장 질의)에서도 뽑는다 —
    # "Jack Jang으로 작성된 문서를 찾아줘"처럼 질문에 한글 이름이 아예 없는 경우,
    # _expand_search_query가 이미 "장승혁"을 keywords에 추론해서 넣어주므로
    # (query_expansion.py의 인물 표기 축) 그걸 그대로 활용한다 — 실측: 이 질문의
    # core는 "Jack Jang 작성 문서"(한글 이름 없음)였지만 keywords엔 "장승혁"이
    # 포함돼 있었음(2026-09-17, 사용자가 "Jack Jang으로 작성된 문서를 찾아줘"로
    # 재현시켜 발견).
    #
    # **하지만 이것도 claude -p 샘플링 비결정성 때문에 매번 성공하지 않는다**
    # (실측: "jack jang이 누구야"를 반복하면 확장 키워드에 "장승혁"이 들어갈 때도,
    # 안 들어갈 때도 있음 — 사용자가 직접 3번 이상 재현시킴). "Jack Jang"은
    # Seunghyeok과 음성적 연관이 없는 사내 지정 영문 이름이라 애초에 LLM이 매번
    # 안정적으로 추측할 수 있는 종류가 아니다. 그렇다고 이름마다 하드코딩 별칭을
    # 추가하는 것도 사용자가 명시적으로 반려함("이렇게 하드코딩하지 말라고") — 대신
    # `lookup_cached_person_in_text()`로 **이 세션에서 이미 한 번이라도 정확한
    # 이름(한글)으로 찾아낸 적 있는 인물인지** 먼저 확인한다(네트워크 호출 없음,
    # atlassian_mcp_client.py의 `_person_alias_cache` 참고) — find_author_id_by_title()이
    # 성공할 때마다 그 계정의 실제 표시 이름까지 자동으로 기억해두므로, 예를 들어
    # "장승혁"으로 먼저 한 번 찾아진 뒤로는 "jack jang이 누구야"만 물어도 코드
    # 수정 없이 바로 풀린다(이름별 수작업이 아니라 실제 조회 결과 재사용).
    person_started = time.monotonic()
    cached = lookup_cached_person_in_text(question) or lookup_cached_person_in_text(expanded_keywords)
    if cached:
        anchor_author_id, anchor_author_name = cached
    elif expanded_person:
        # 예전엔 여기서 "원본/확장 검색어 중 한글 2~4음절 토큰"을 전부 사람 이름
        # 후보로 취급해 순서대로 find_author_id_by_title()에 넣어봤는데, "파트"/
        # "인원"/"생성"/"작성" 같은 흔한 명사까지 그 정규식(re.fullmatch(r"[가-힣]
        # {2,4}"))에 걸려서 완전히 무관한 사람이 anchor로 잡히는 사고가 실측됨
        # (2026-09-18: "EnergySW 파트 인원을 한정하여 각각의 인원이 얼마나 많은
        # page를 생성했는지" 질문에서 첫 후보였던 "파트"가 "OOO 파트 업무
        # 진행사항" 문서를 여러 건 쓴 다른 팀 사람에게 우연히 매칭되어, 질문과
        # 전혀 상관없는 그 사람의 문서 200건이 통째로 답변 컨텍스트에 실림 —
        # 사용자가 "왜 상관없는 OOO님이 답변으로 나오지?"로 재현). 이제는 질문에
        # 실제로 특정 인물 한 명이 지목됐는지 자체를 이미 하고 있던 검색어 확장
        # LLM 호출의 "person" 필드로 판별한다(query_expansion.py 참고) — 질문이
        # "EnergySW 인원 전체" 같은 그룹을 묻거나 사람 이름이 아예 없으면 그
        # 호출이 person을 null로 주므로, 여기 elif 자체를 안 타서 anchor 오염이
        # 구조적으로 불가능해진다.
        anchor_author_id, anchor_author_name = find_author_id_by_title(expanded_person)
        if not anchor_author_id:
            # person 필드에 조사가 안 떨어진 채로 나온 경우 대비 재시도(2026-09-18,
            # 사용자 실측: "심철로된 jira티켓 검색..."을 5번 돌리면 1번꼴로
            # person="심철로"(조사 "로" 미제거)가 나와 계정을 못 찾았음 — "오타
            # 아닌데"로 재현됨). 정규식으로 조사를 떼는 안전망은 "지은"/"수은"처럼
            # 실제 이름과 겹치는 조사가 많아 사용자가 반려했다("하드코딩하지말고
            # 프롬프트엔지니어링을... 라이브러리있니?") — 대신 같은 claude -p를
            # 캐시 우회해서 한 번 더 불러 새 샘플을 받는다(retry_person_extraction()
            # 독스트링 참고). 실패가 흔치 않은 경로에서만 도는 재시도라 평소
            # 지연에는 영향 없음.
            retried_person = retry_person_extraction(question, history)
            if retried_person and retried_person != expanded_person:
                anchor_author_id, anchor_author_name = find_author_id_by_title(retried_person)
    else:
        anchor_author_id = anchor_author_name = None
    if cached or expanded_person:
        tr.note(
            "person", "인물 계정 확인",
            f"{anchor_author_name or expanded_person} → 계정 확인" if anchor_author_id else f"“{expanded_person}” 계정을 찾지 못함",
            status="done" if anchor_author_id else "error",
            items=["이전에 찾은 인물 캐시 사용"] if cached else None,
            ms=int((time.monotonic() - person_started) * 1000),
        )

    person_count_note = None  # wants_full_list일 때만 채워짐, parts에 별도로 붙임(아래 참고)
    if anchor_author_id:
        # "심철로 할당된 jira 티켓 찾아줘"(사용자 요청, 2026-09-18: "Jira 티켓도
        # 연동할수있어?") 같은 질문은 Confluence 문서 개수/목록 질문과 완전히
        # 다른 데이터 소스가 필요하다 — search_jira_assigned()는 assignee 필드로
        # "정확히" 매칭하므로(search_jira_mentions()의 text~ 다수결/LLM 판단과
        # 달리) anchor_author_id만 맞으면 그대로 신뢰할 수 있다. Confluence
        # 개수/목록 로직과 섞이지 않게 먼저 분기해서 처리하고 아래로 안 내려간다.
        wants_jira_tickets = jira_assign_intent  # 위 jira_direct 판단과 같은 기준(티켓·이슈·할당·assign)
        if wants_jira_tickets:
            with tr.step("jira", "Jira 담당 이슈 검색") as st:
                tickets, is_complete = search_jira_assigned(anchor_author_id)
                st.detail = f"진행 중 이슈 {len(tickets)}건" + ("" if is_complete else " (상한 도달)")
            display_name = anchor_author_name
            note = (
                f"[참고: Jira에서 {display_name or '이 인물'}님에게 assignee로 할당된 "
                f"이슈 중 완료(Done)·취소(Cancelled)를 뺀 진행 중인 이슈를 우선순위 "
                f"높은 순 → 마감일 이른 순으로 정렬해 검색한 결과 {len(tickets)}건이 "
                f"확인되었습니다."
            )
            if not is_complete:
                note += " 결과가 한 번에 가져올 수 있는 상한에 걸려, 실제로는 이보다 더 있을 수 있는 최소값입니다."
            note += (
                " 완료/취소된 과거 이슈는 이 집계에서 의도적으로 제외했습니다(전체 "
                "이력이 필요하면 별도로 말씀해 주세요를 답변에 덧붙이세요). "
                "답변할 때 이 개수와 아래 목록(우선순위·마감일 포함)을 그대로 쓰세요.]"
            )
            person_count_note = note
            seen_urls = {p["url"] for p in live_pages}
            for t in tickets:
                if t["url"] in seen_urls:
                    continue
                detail_bits = [b for b in [
                    f"우선순위: {t['priority']}" if t["priority"] else "",
                    f"마감일: {t['duedate']}" if t["duedate"] else "",
                    f"상태: {t['status']}" if t["status"] else "",
                ] if b]
                live_pages.append({
                    "title": f"[{t['key']}] {t['title']}",
                    "url": t["url"],
                    "text": " / ".join(detail_bits),
                    "type": "issue",
                    "author_display_name": display_name,
                    "source": "jira_assigned",
                })
                seen_urls.add(t["url"])
        else:
            # "장승혁이 만든 문서 다 보여줘" / "장승혁이 몇 개 썼어" 같은 질문은
            # 일반 인물 질문과 다르게 취급해야 한다 — 위 anchor 보강은 답변에
            # 곁들일 예시 문서 몇 개를 찾는 용도라 6건에서 끊지만(added >= 6), 이
            # 질문들은 "개수/전체 목록" 자체가 답이라 6건 상한을 걸면 답이 틀린다
            # (실측: EnergySW 스페이스에서 상위 작성자는 개인당 페이지가 수백
            # 건까지 나옴, 2026-09-18 전수조사로 확인). search_by_creator의 CQL
            # 총량은 200건에서 하드 컷되므로(atlassian_mcp_client.py의
            # search_by_creator 문서 참고) "정확한 개수"가 아니라 "최소 이만큼"
            # 으로만 말할 수 있는 경우가 있다 — 200건을 다 채워서 돌아오면 그
            # 사실을 답변에 명시하게 한다.
            wants_full_list = bool(
                expanded_wants_stats
                or _PERSON_DOC_COUNT_RE.search(question)
                or _PERSON_DOC_LISTALL_RE.search(question)
            )
            creator_limit = 200 if wants_full_list else 10
            with tr.step("creator", "작성자 문서 검색 (CQL)") as st:
                author_pages, author_display_name = search_by_creator(anchor_author_id, limit=creator_limit)
                st.detail = f"{len(author_pages)}건" + (" · 전수 집계" if wants_full_list else " · 예시용 상위만 사용")
            author_display_name = author_display_name or anchor_author_name
            seen_urls = {p["url"] for p in live_pages}

            if wants_full_list:
                truncated = len(author_pages) >= creator_limit
                count_note = (
                    f"[참고: Confluence에서 {author_display_name or anchor_author_name}님이 "
                    f"작성(creator)한 문서를 CQL로 전수 검색한 결과 {len(author_pages)}건이 "
                    f"확인되었습니다."
                )
                if truncated:
                    count_note += (
                        " Confluence 검색 API가 결과를 200건에서 잘라 반환하는 한계가 있어, "
                        "실제로는 이보다 더 많을 수 있는 최소값입니다."
                    )
                count_note += " 답변할 때 이 숫자를 그대로 쓰고, 목록도 아래 출처를 근거로 답하세요.]"
                # live_pages에 안 넣고 따로 들고 있다가 parts에만 붙인다(아래 return
                # 직전) — live_pages는 sources(UI에 노출되는 출처 카드 목록)도 같이
                # 만드는데, 이 메모는 실제 문서가 아니라 URL이 없어서 sources에
                # 섞이면 빈 링크 카드가 뜬다.
                person_count_note = count_note
                for p in author_pages:  # 목록/개수 질문이므로 6건 상한 없이 전부 포함
                    if p["url"] in seen_urls:
                        continue
                    if author_display_name:
                        p["author_display_name"] = author_display_name
                    # 최대 200건까지 들어올 수 있어 excerpt까지 다 넣으면 컨텍스트가
                    # 너무 커진다 — 이 분기는 "개수/목록"이 목적이라 제목+URL만으로
                    # 충분하므로 본문 발췌는 비운다.
                    p["text"] = ""
                    p["source"] = "confluence_cql"
                    live_pages.append(p)
                    seen_urls.add(p["url"])
            else:
                added = 0
                for p in author_pages:
                    if p["url"] in seen_urls:
                        continue
                    if author_display_name:
                        p["author_display_name"] = author_display_name
                    p["source"] = "confluence_cql"
                    live_pages.append(p)
                    seen_urls.add(p["url"])
                    added += 1
                    if added >= 6:  # 발췌만 쓰지만 소스 목록이 너무 길어지지 않게 상한
                        break
    elif team_stats_intent:
        # anchor_author_id가 없다는 건(위 elif expanded_person 분기를 안 탔다는 뜻)
        # 질문이 특정 인물 한 명을 지목한 게 아니라는 뜻인데(query_expansion.py의
        # person 필드가 null), 그런데도 "몇 개/다 보여줘"·"가장 많이 쓴 사람" 같은
        # 개수·순위·목록 의도는 감지됐다(정규식 또는 expanded_wants_stats LLM 판단,
        # 2026-09-22 추가 — "가장많이 작성한 사람은 누구야"가 기존 정규식 어디에도
        # 안 걸려서 그냥 일반 문서검색으로 새버린 사고로 발견, query_expansion.py의
        # wants_person_stats 필드 설명 참고). "EnergySW 파트 인원을 한정하여 각각의
        # 인원이 얼마나 많은 page를 생성했는지"처럼 특정 인물이 아니라 "우리 팀 전체 각자"를 묻는
        # 질문이 정확히 이 조합이다(사용자가 이전 답변에 "이게 맞는 답이라고
        # 생각하니?"로 지적, 2026-09-18 — 그때는 이 elif 자체가 없어서 "한 명씩
        # 물어보세요"로만 답했었음). 이 위키봇이 다루는 유일한 "팀 전체"가
        # EnergySW 파트이므로, Confluence 명단 문서에서 실시간으로 읽은 이름들을
        # 한 명씩 순회하며 집계한다 — 이름을 코드에 하드코딩하지 않는다
        # (get_energysw_roster() 독스트링 참고).
        #
        # 인원 수만큼 순차로 Confluence를 호출해야 해서 수십 초~분 단위로 느리다
        # (ThreadPoolExecutor 병렬화는 과거에 동일 세션 동시요청이 응답을 뒤섞는
        # 사고를 낸 전례가 있어 일부러 안 씀 — atlassian_mcp_client.py의
        # search_confluence_live 근처 주석 참고: "인원/조직 데이터처럼 실수가
        # 그대로 신뢰 문제로 이어지는 내용을 다루므로... 정확성이 우선". 같은
        # 원칙이 여기 인원별 집계에도 그대로 적용된다).
        roster_started = time.monotonic()
        roster = get_energysw_roster()
        wants_team_jira = jira_assign_intent
        if roster and wants_team_jira:
            # 팀 전체 Jira 할당 순위 — 인원별로 계정을 찾고, assignee = 계정인 진행 중 이슈 수를 센다.
            # 인물 한 명용 분기(search_jira_assigned)와 같은 정의(완료·취소 제외)를 그대로 쓴다.
            tr.push({"type": "step", "key": "jira_roster", "label": "팀 명단 인원별 Jira 할당 집계", "status": "running",
                     "detail": f"{len(roster)}명 순회 중"})
            rows = []
            for person_name in roster:
                pid, pdisplay = find_author_id_by_title(person_name)
                if not pid:
                    rows.append((person_name, None, None, True))
                    continue
                tickets, is_complete = search_jira_assigned(pid)
                # 명단의 한글 이름을 앞에, Atlassian 표시 이름(영문 계정명)을 괄호로 — 답변에서 누군지 바로 알아보게
                shown = person_name if not pdisplay or pdisplay == person_name else f"{person_name}({pdisplay})"
                rows.append((shown, pid, len(tickets), is_complete))
            rows.sort(key=lambda r: -(r[2] or 0))
            lines = []
            seen_urls = {p["url"] for p in live_pages}
            for name_shown, pid, cnt, complete in rows:
                if pid is None:
                    lines.append(f"- {name_shown}: 계정을 찾지 못해 확인 불가")
                    continue
                url = _jira_assigned_jql_url(pid)
                lines.append(f"- {name_shown}: 진행 중 할당 {cnt}건" + ("" if complete else " 이상(상한 도달)") + f" | URL: {url}")
                if cnt and url not in seen_urls:
                    live_pages.append({
                        "title": f"{name_shown} — Jira 진행 중 할당 이슈 {cnt}건" + ("" if complete else "+"),
                        "url": url, "text": "", "type": "issue", "source": "jira_assigned",
                        "author_display_name": name_shown,
                    })
                    seen_urls.add(url)
            person_count_note = (
                f'[참고: Confluence "{_ROSTER_PAGE_TITLE}" 명단({len(roster)}명) 기준으로, 각자에게 '
                "Jira에서 assignee로 할당된 이슈 중 완료(Done)·취소(Cancelled)를 뺀 진행 중 이슈 수를 "
                "한 명씩 JQL로 집계한 결과입니다(많은 순). 답변할 때 이 목록을 표/불릿으로 그대로 옮기고 "
                "숫자를 새로 세거나 바꾸지 마세요. 각 줄의 URL은 그 사람의 진행 중 이슈 목록을 Jira에서 "
                "여는 링크이니 출처로 그대로 쓰세요. 완료된 과거 이슈까지 포함한 통산 개수가 아니라는 점도 "
                "밝히세요.]\n" + "\n".join(lines)
            )
            tr.note("jira_roster", "팀 명단 인원별 Jira 할당 집계",
                    f"{len(roster)}명 집계 완료 · 1위 {rows[0][0]} {rows[0][2] or 0}건" if rows else "집계 결과 없음",
                    ms=int((time.monotonic() - roster_started) * 1000))
        elif roster:
            tr.push({"type": "step", "key": "roster", "label": "팀 명단 인원별 집계", "status": "running",
                     "detail": f"{len(roster)}명 순회 중"})
            rows = []
            for person_name in roster:
                pid, pdisplay = find_author_id_by_title(person_name)
                if not pid:
                    rows.append((person_name, None, 0))
                    continue
                ppages, pdisp2 = search_by_creator(pid, limit=200)
                rows.append((pdisp2 or pdisplay or person_name, len(ppages), len(ppages) >= 200))
            rows.sort(key=lambda r: -(r[1] or 0))  # 계정 못 찾은 사람(count=None)은 0 취급으로 뒤로
            lines = []
            for row in rows:
                if row[1] is None:
                    lines.append(f"- {row[0]}: 계정을 찾지 못해 확인 불가")
                    continue
                name_shown, cnt, trunc = row
                lines.append(f"- {name_shown}: {cnt}건" + ("(200건 상한 도달, 하한선)" if trunc else ""))
            person_count_note = (
                f'[참고: Confluence "{_ROSTER_PAGE_TITLE}" 명단({len(roster)}명) 기준으로, '
                "각자가 creator인 Confluence 문서를 CQL로 한 명씩 전수 검색한 결과입니다 "
                "(CQL 특성상 200건에서 잘리므로 그 값이 찍힌 사람은 정확한 개수가 아니라 "
                "최소값). 답변할 때 이 목록을 표/불릿으로 그대로 옮기고 숫자를 새로 세거나 "
                "바꾸지 마세요.]\n" + "\n".join(lines)
            )
            tr.note("roster", "팀 명단 인원별 집계", f"{len(roster)}명 집계 완료",
                    ms=int((time.monotonic() - roster_started) * 1000))
        else:
            tr.note("roster", "팀 명단 인원별 집계", "명단 문서를 읽지 못함", status="error",
                    ms=int((time.monotonic() - roster_started) * 1000))

    # 본문이 진짜로 비어있는 페이지(다이어그램/엑셀 첨부파일만 있음)는 원본 첨부파일을
    # 직접 파싱해서 보완한다(_fetch_attachment_text 참고, 사용자 확정 2026-08-12).
    empty_pages = [p for p in live_pages if p.get("text") == _EMPTY_BODY_NOTE]
    attach_started = time.monotonic()
    attach_items = []
    for p in empty_pages:
        if p.get("text") == _EMPTY_BODY_NOTE:
            m = _PAGE_ID_IN_URL_RE.search(p.get("url", ""))
            if not m:
                continue
            extracted_text, source_kind = _fetch_attachment_text(m.group(1))
            if not extracted_text:
                attach_items.append(f"{p['title']}: 첨부 없음")
                continue
            attach_items.append(f"{p['title']}: {source_kind}에서 {len(extracted_text):,}자")
            if source_kind == "drawio 다이어그램":
                caveat = (
                    "도형 좌표 기준으로 정렬했으나 alt/loop 같은 중첩 프레임 구조상 완벽한 "
                    "시간순 재현은 아닐 수 있습니다 — 각 항목은 실제 다이어그램에 있는 내용이 "
                    "맞지만, 단계 순서는 이 목록의 나열 순서를 그대로 확신하지 말고 register/명령 "
                    "이름의 논리적 흐름으로 재구성해서 답하세요."
                )
            else:
                caveat = (
                    "시트/행 순서 그대로 옮긴 것이라 병합 셀이나 서식으로만 표현된 정보(예: "
                    "그룹 경계선)는 텍스트에 안 드러날 수 있습니다 — 표 구조를 보수적으로 "
                    "해석하고, 확실치 않은 셀 대응은 추측 없이 원본 확인을 권하세요."
                )
            p["text"] = (
                f"(아래는 {source_kind} 원본(페이지 본문 자체는 비어있음)에서 자동 추출한 "
                f"내용입니다. {caveat})\n" + extracted_text
            )
    if empty_pages:
        tr.note("attachments", "본문 없는 페이지 첨부파일 파싱", f"{len(empty_pages)}개 페이지",
                items=attach_items, ms=int((time.monotonic() - attach_started) * 1000))

    if not using_rovo and not live_pages:
        # `and not live_pages` 추가(2026-09-18) — 원래 `using_rovo`만 보고 폴백하면,
        # 위 인물 anchor 보강(search_by_creator)이 뭔가를 이미 채워 넣은 뒤라도
        # live_pages를 통째로 CQL 폴백 결과로 덮어써서 애써 찾은 인물 문서가 전부
        # 날아간다 — 실측: "정지석이 몇 개 문서 썼어"처럼 사람+개수 질문은 원본
        # 문장 그대로 rovo_search(original_query)를 돌리면(맨 위 live_pages 초기화
        # 지점) 텍스트 매칭할 본문 키워드가 없어 0건이 나오기 쉬워(using_rovo=False)
        # 이 경로를 그대로 타는데, 그 시점엔 이미 anchor 보강으로 author_pages가
        # live_pages에 들어가 있는 상태라 여기서 지워버리면 개수/목록 답변 자체가
        # 통째로 사라진다. `using_rovo`가 원래 의도한 건 "Rovo/MCP 자체가 아예
        # 실패했을 때"의 폴백이므로(2026-08-19 주석 참고), 이미 뭔가 채워진
        # live_pages는 안 건드리는 게 맞다.
        # 스페이스를 7개로 넓힌 뒤로 동일 키워드 매치 건수가 훨씬 많아져서(예: "TOU" 20+건)
        # limit=3이면 진짜 관련 문서가 순위 밖으로 밀릴 위험이 커짐 -> 여유 있게 5개
        with tr.step("cql", "Confluence CQL 검색 (폴백)") as st:
            live_pages = search_confluence_live(expanded_keywords, limit=5)
            st.detail = f"{len(live_pages)}건"
            st.items = [p["title"] for p in live_pages]
        for p in live_pages:
            p["source"] = "confluence_cql"

    parts = []
    sources = []

    # 소스 라벨을 item마다 정확히 표시한다(2026-09-18 수정) — 예전엔 이 요청
    # 전체가 Rovo Search를 한 번이라도 썼는지(using_rovo, 요청당 값 1개)만 보고
    # "모든" 항목에 똑같이 "(live, Rovo Search)"를 붙였다. 그런데 anchor 보강
    # (search_by_creator, CQL)이나 Jira 할당 이슈(search_jira_assigned)로 채워진
    # 항목은 실제로 Rovo Search를 거치지 않았는데도 라벨만 "Rovo Search"로 찍혀서
    # 사용자가 "심철로 할당된 jira 티켓 찾아줘" 결과를 보고 출처 표기가 이상하다고
    # 지적함. 이제 각 항목을 live_pages에 넣는 시점에 `p["source"]`를 직접 붙이고
    # (rovo/confluence_cql/jira_assigned), 여기서는 그 값만 보고 라벨을 고른다 —
    # source가 없는 항목(예전 경로가 누락했을 가능성 대비)은 using_rovo로 안전하게
    # 폴백한다.
    _SOURCE_LABELS = {
        "rovo": "Rovo Search",
        "confluence_cql": "CQL 작성자 검색",
        "jira_assigned": "Jira 담당자 검색",
    }
    for p in live_pages:
        is_issue = p.get("type") == "issue"
        kind = "Jira" if is_issue else "Confluence"
        label_type = "jira" if is_issue else "confluence"
        owner = CONFLUENCE_PERSONAL_SPACE_OWNERS.get(_confluence_space_of(p.get("url")))
        author_name = p.get("author_display_name")
        if owner:
            owner_note = f", {owner}님의 개인 Confluence 스페이스 문서"
        elif author_name and is_issue:
            # search_jira_assigned()로 채워진 이슈 — "작성한 문서"가 아니라
            # "할당된 이슈"이므로 문구를 구분한다(2026-09-18).
            owner_note = f", {author_name}님에게 할당된 이슈"
        elif author_name:
            # search_by_creator()로 보강된 문서 — 발췌만 있어 본문이 짧을 수 있으니
            # 모델이 "이 사람이 작성한 문서"라는 맥락을 놓치지 않게 명시(2026-09-17).
            owner_note = f", {author_name}님이 작성한 문서(짧은 발췌)"
        else:
            owner_note = ""
        source_label = _SOURCE_LABELS.get(p.get("source")) or ("Rovo Search" if using_rovo else None)
        source_tag = f"live, {source_label}" if source_label else "live"
        parts.append(f"[출처: {kind}({source_tag}{owner_note}) - {p['title']} | URL: {p['url']}]\n{p['text']}")
        sources.append({"type": label_type, "label": p["title"], "url": p["url"]})

    if person_count_note:
        parts.append(person_count_note)

    context = "\n\n---\n\n".join(parts)
    tr.note("context", "참고 자료 구성", f"문서 {len(sources)}개 · {len(context):,}자",
            status="done" if sources else "error")
    return context, sources


OLLAMA_URL = "http://localhost:11434"
DEFAULT_OLLAMA_MODEL = os.environ.get("WIKIBOT_OLLAMA_MODEL", "exaone3.5:2.4b")


def list_ollama_models():
    try:
        with urllib.request.urlopen(f"{OLLAMA_URL}/api/tags", timeout=3) as resp:
            data = json.load(resp)
        return [m["name"] for m in data.get("models", [])]
    except Exception:
        return []


def _build_messages(history, context):
    messages = list(history[:-1])  # 마지막 메시지(현재 질문)는 컨텍스트를 붙여서 재구성
    last_question = history[-1]["content"]
    messages.append({
        "role": "user",
        "content": (
            f"=== 참고 자료 ===\n{context}\n\n"
            f"=== 질문 ===\n{last_question}"
        ),
    })
    return messages


def call_claude(history, context):
    import anthropic
    client = anthropic.Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])
    msg = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=2048,
        system=SYSTEM_PROMPT,
        messages=_build_messages(history, context),
    )
    return msg.content[0].text


def call_ollama(history, context, model):
    payload = {
        "model": model,
        "messages": [{"role": "system", "content": SYSTEM_PROMPT}] + _build_messages(history, context),
        "stream": False,
        # Ollama 기본 num_ctx=2048은 위키 컨텍스트(보통 3천~1만+자)를 넣으면
        # 앞부분(시스템 프롬프트+가장 관련도 높은 청크)이 통째로 잘려나가 근거 없는
        # 답변(환각)이 나온다 — 반드시 넉넉하게 키워줘야 함.
        # 16384로 키워봤지만(큰 컨텍스트 대응) GPU 4GB에서 KV캐시가 넘쳐 18%/82%
        # CPU/GPU로 갈라지며 속도만 3배 느려지고 환각은 그대로였음(실측) -> 되돌림.
        # 진짜 원인은 컨텍스트 절대량보다 "표 형태 정보가 잡담성 텍스트에 묻히는 것"에
        # 가까움 -> build_context에서 노이즈 청크 필터링 + full-page fetch 개수 축소로 대응
        "options": {"num_ctx": 8192},
    }
    req = urllib.request.Request(
        f"{OLLAMA_URL}/api/chat",
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=300) as resp:
        data = json.load(resp)
    return data["message"]["content"]


def _ollama_available():
    try:
        urllib.request.urlopen(f"{OLLAMA_URL}/api/tags", timeout=2)
        return True
    except Exception:
        return False


# Rovo Chat(Confluence 내장 AI)의 "답변 합성" 자체는 비공개 API라 재사용 불가 —
# 공식으로 노출된 건 Rovo Search(검색)뿐이라 그건 이미 build_context()에서 쓰고 있음.
# 대신 Rovo Search가 찾아온 컨텍스트를 로컬 소형 모델이 아니라 Claude로 합성하면
# Rovo Chat에 근접한 답변 품질이 나온다는 판단(사용자 확정) -> 답변 합성도 항상
# `claude -p` CLI로 라우팅한다. 별도 API 결제 없이 로그인된 Claude Pro 세션으로
# 동작하지만(= Claude Code 사용량/속도제한 차감), 매 질문마다 호출됨을 감안할 것.


def _claude_cli_available():
    return shutil.which("claude") is not None


def call_claude_cli(history, context, timeout=180, model="sonnet"):
    messages = _build_messages(history, context)
    # claude -p는 role 배열이 아니라 프롬프트 문자열 하나만 받으므로 이전 대화를
    # 텍스트로 펼쳐서 넣는다 (그림 요청은 보통 짧은 후속 질문이라 부담 적음)
    parts = []
    for m in messages[:-1]:
        speaker = "사용자" if m["role"] == "user" else "위키봇"
        parts.append(f"[{speaker}] {m['content']}")
    parts.append(messages[-1]["content"])
    prompt = "\n\n".join(parts)
    return _claude_cli_raw(prompt, SYSTEM_PROMPT, model=model, timeout=timeout)


def _claude_cli_raw(prompt, system_prompt, model="sonnet", timeout=180):
    cmd = [
        "claude", "-p", prompt,
        "--model", model,
        "--output-format", "json",
        "--system-prompt", system_prompt,
        # --disallowedTools(차단 목록)는 mcp__atlassian__* 같은 MCP 도구는 안 걸러서,
        # 모델이 스스로 그 도구를 호출하려다 비대화형(-p) 모드라 권한 승인을 받지
        # 못하고 막히는 사고가 실측됨("도구 권한이 승인되지 않아... 이 세션은
        # non-interactive라서..."). 컨텍스트에 이미 필요한 자료를 다 넣어주므로
        # 답변 합성 단계엔 도구가 전혀 필요 없다 -> 전부 비활성화.
        "--tools", "",
    ]
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    if result.returncode != 0:
        raise RuntimeError(f"claude CLI exit={result.returncode}: {result.stderr[:500]}")
    data = json.loads(result.stdout)
    if data.get("is_error"):
        if looks_like_usage_limit_error(data.get("result")):
            mark_usage_exhausted(data.get("result"))
        raise RuntimeError(f"claude CLI error: {data.get('result')}")
    return data["result"]


def generate_answer(history, context, model=None):
    """(답변, backend) 튜플을 반환한다. backend는 _ensure_real_images_shown을
    적용할지 판단하는 데 쓰인다 — claude -p/API는 이미지 유무를 스스로 정확히
    판단하므로 그 판단을 후처리로 덮어쓰면 안 되고, 이 안전장치는 원래 취지대로
    작은 로컬 모델(ollama) 답변에만 적용해야 한다.

    model은 작업대 모델 선택값(model_arena.CLAUDE_MODELS의 "claude:<alias>" 또는
    "ollama:<이름>")이다. 접두사 없는 예전 값(ollama 모델명)이나 None이면 원래 순서대로
    claude CLI → API → ollama 폴백을 탄다."""
    if model and model.startswith("claude:"):
        alias = model.split(":", 1)[1]
        if _claude_cli_available():
            return call_claude_cli(history, context, model=alias, timeout=300 if alias == "opus" else 180), "claude_cli"
        if os.environ.get("ANTHROPIC_API_KEY"):
            return call_claude(history, context), "claude_api"
        raise RuntimeError("claude CLI/ANTHROPIC_API_KEY가 없어 Claude 모델을 쓸 수 없습니다.")
    if model and model.startswith("ollama:"):
        return call_ollama(history, context, model.split(":", 1)[1]), "ollama"

    if _claude_cli_available():
        try:
            return call_claude_cli(history, context), "claude_cli"
        except Exception as e:
            print(f"⚠️  claude CLI 실패, 폴백: {e}", file=sys.stderr)

    if os.environ.get("ANTHROPIC_API_KEY"):
        return call_claude(history, context), "claude_api"
    if _ollama_available():
        return call_ollama(history, context, model or DEFAULT_OLLAMA_MODEL), "ollama"
    return (
        "⚠️ ANTHROPIC_API_KEY도 없고 로컬 Ollama 서버도 응답하지 않습니다. "
        "`ollama serve`를 실행하거나 ANTHROPIC_API_KEY를 설정해주세요.\n\n"
        "--- 참고로 찾은 자료 ---\n" + context
    ), "none"


@app.route("/")
def index():
    if ADMIN_MODE:
        return redirect(url_for("admin_home"))
    # URL은 로그인 여부와 무관하게 항상 하나(/)다 — ChatGPT처럼 신원 구분은 URL이 아니라
    # 세션 쿠키가 한다. 같은 /로 들어와도 로그인된 브라우저는 자기 쿠키 기반으로
    # /api/conversations 등이 자기 데이터만 걸러 보여주고, 로그인 안 한(또는 다른) 브라우저는
    # 그 쿠키가 없으니 공용/자기 자신의 화면만 본다(2026-09-22, /app 분리 시도 되돌림).
    return send_from_directory(STATIC_DIR, "index.html")


@app.route("/wiki-images/<path:filename>")
def wiki_images(filename):
    return send_from_directory(DOCS_IMAGES_DIR, filename)


@app.route("/confluence-images/<path:filename>")
def confluence_images(filename):
    return send_from_directory(LIVE_DIAGRAM_DIR, filename)


# 로컬 위키 이미지(images/파일명)와 라이브 Confluence에서 받아온 이미지
# (confluence-images/파일명) 두 경로 프리픽스를 모두 인식한다
_IMG_MD = re.compile(r'!\[[^\]]*\]\((?:\./)?(?:images|confluence-images)/[^)]+\)')
_IMG_MD_PATH = re.compile(r'(!\[[^\]]*\]\()(?:\./)?(images|confluence-images)/([^)]+)(\))')
_NO_IMAGE_CLAIM = re.compile(r'^.*위키에?\s*저장된?\s*그림은?\s*없어[^\n]*\n?', re.MULTILINE)

_IMG_ROUTE = {"images": "wiki-images", "confluence-images": "confluence-images"}


def _fix_image_paths(answer):
    """답변 속 이미지 마크다운(images/, confluence-images/)을 실제로 서빙되는 경로로 변환."""
    return _IMG_MD_PATH.sub(lambda m: f"{m.group(1)}/{_IMG_ROUTE[m.group(2)]}/{m.group(3)}{m.group(4)}", answer)


def _ensure_real_images_shown(answer, context):
    """모델이 컨텍스트에 실제로 있는 이미지를 놓치고 mermaid로 지어내는 경우가 있어
    (특히 작은 로컬 모델), 실제 위키 이미지가 컨텍스트에 있으면 모델 판단에 기대지 않고
    프로그래밍적으로 항상 보여준다."""
    real_images = list(dict.fromkeys(_IMG_MD.findall(context)))
    if not real_images:
        return answer
    if _IMG_MD.search(answer):
        return answer  # 모델이 이미 실제 이미지를 포함시킴
    answer = _NO_IMAGE_CLAIM.sub('', answer)
    shown = "\n".join(real_images[:2])
    return f"{answer.rstrip()}\n\n관련 이미지:\n{shown}"


JUDGE_MODEL = os.environ.get("WIKIBOT_JUDGE_MODEL", "haiku")
COMPARE_DEFAULT = [m.strip() for m in os.environ.get("WIKIBOT_COMPARE_MODELS", "claude:sonnet,claude:haiku").split(",") if m.strip()]
MAX_COMPARE_MODELS = 3


def _default_model_id():
    if _claude_cli_available() or os.environ.get("ANTHROPIC_API_KEY"):
        return "claude:sonnet"
    ollama = list_ollama_models()
    return f"ollama:{ollama[0]}" if ollama else "claude:sonnet"


@app.route("/api/models")
def models():
    ollama_models = list_ollama_models()
    claude_ok = _claude_cli_available() or bool(os.environ.get("ANTHROPIC_API_KEY"))
    catalog = [dict(m, available=claude_ok) for m in model_arena.CLAUDE_MODELS]
    catalog += [{"id": f"ollama:{n}", "label": n, "note": "로컬 Ollama", "available": True} for n in ollama_models]
    return jsonify({
        # 예전 프론트 호환 필드
        "models": ollama_models,
        "default": DEFAULT_OLLAMA_MODEL,
        "claude_available": bool(os.environ.get("ANTHROPIC_API_KEY")),
        # 작업대용
        "catalog": catalog,
        "default_model": _default_model_id(),
        "compare_default": COMPARE_DEFAULT,
        "judge_model": model_arena.model_label(f"claude:{JUDGE_MODEL}"),
        "adopt_margin": model_arena.ADOPT_MARGIN,
    })


def _identity():
    """(user_id, anon_session_id). 관리자 인스턴스는 소유자 구분 없이 (None, None)."""
    if ADMIN_MODE:
        return None, None
    return _current_identity()


def _owned_by(row, user_id, anon_id):
    if ADMIN_MODE:
        return True
    if user_id:
        return row.get("user_id") == user_id
    return row.get("anon_session_id") == anon_id


# 지금 이 서버 프로세스에서 답을 만들고 있는 실행들(run_id -> {"conversation_id", "tracer"}).
# 페이지를 새로고침하거나 대화를 다시 열어도 진행 중인 답변을 이어서 보여주고(/api/runs/<id>가
# 여기서 실시간 이벤트를 꺼내 줌), 같은 대화에 질문이 겹쳐 들어오는 것을 막는 데 쓴다.
# 2026-10-07 실측: 3분 넘게 걸리는 팀 명단 집계 질문 도중 새로고침하면 화면에 진행 표시가 없어
# 같은 질문을 두 번 더 보내, 한 대화에 같은 질문·답변·실행 기록이 3개씩 쌓였다.
_LIVE_RUNS = {}
_LIVE_RUNS_LOCK = threading.Lock()


def _live_run_for_conversation(conversation_id):
    with _LIVE_RUNS_LOCK:
        for run_id, info in _LIVE_RUNS.items():
            if info["conversation_id"] == conversation_id:
                return run_id
    return None


class _ChatError(Exception):
    def __init__(self, message, code=400):
        super().__init__(message)
        self.code = code


def _prepare_chat(body, user_id, anon_id):
    """요청 검증 + 대화 확보까지 동기적으로 처리한다(스트리밍 시작 전에 400/404를 돌려주기 위함)."""
    history = body.get("history", [])
    if not history or history[-1].get("role") != "user":
        raise _ChatError("history의 마지막 메시지는 role=user 여야 합니다.")
    conversation_id = body.get("conversation_id") or None
    # 그림 문법 오류로 같은 turn을 더 큰 모델로 재시도하는 호출(프론트 pickEscalationModel) —
    # 이때는 사용자 메시지를 또 저장하거나 답변 행을 새로 쌓지 않고 직전 답변만 덮어쓴다.
    retry = bool(body.get("retry"))
    is_new = not conversation_id
    if is_new:
        conversation_id = chat_history.create_conversation(history[-1]["content"], user_id=user_id, anon_session_id=anon_id)
    else:
        # 기존 대화에 이어붙이는 경우 — conversation_id를 짐작/재사용해 남의 대화에
        # 메시지를 끼워넣지 못하도록 소유권을 확인한다.
        conv = chat_history.get_conversation(conversation_id, include_deleted=True)
        if conv is None or not _owned_by(conv, user_id, anon_id):
            raise _ChatError("대화를 찾을 수 없습니다.", 404)
    run_id = uuid.uuid4().hex
    with _LIVE_RUNS_LOCK:
        if any(info["conversation_id"] == conversation_id for info in _LIVE_RUNS.values()):
            raise _ChatError("이 대화의 이전 질문에 아직 답하는 중입니다. 답변이 끝난 뒤 다시 보내주세요.", 409)
        _LIVE_RUNS[run_id] = {"conversation_id": conversation_id, "tracer": None}

    model = body.get("model") or None
    candidates = []
    if body.get("compare"):
        candidates = [m for m in (body.get("candidates") or COMPARE_DEFAULT) if isinstance(m, str)]
        candidates = list(dict.fromkeys(candidates))[:MAX_COMPARE_MODELS]
        if model and model in candidates:  # 선택한 모델을 기본(현직) 후보로 맨 앞에
            candidates.remove(model)
            candidates.insert(0, model)
    return {
        "history": history,
        "conversation_id": conversation_id,
        "is_new": is_new,
        "retry": retry,
        "model": model,
        "candidates": candidates if len(candidates) >= 2 else [],
        "file_ids": [f for f in (body.get("file_ids") or []) if isinstance(f, str)][:5],
        "user_id": user_id,
        "anon_id": anon_id,
        "run_id": run_id,
    }


def build_upload_context(file_ids, user_id, anon_id, tracer):
    """첨부 파일의 추출 텍스트를 참고 자료 블록으로 만든다. 반환: (parts, sources)."""
    parts, sources = [], []
    if not file_ids:
        return parts, sources
    with tracer.step("uploads", "첨부 파일 읽기") as st:
        items = []
        for fid in file_ids:
            row = chat_history.get_file(fid)
            if row is None or not _owned_by(row, user_id, anon_id):
                items.append(f"{fid[:8]}…: 찾을 수 없음")
                continue
            text = file_store.read_text(fid)
            if not text:
                items.append(f"{row['filename']}: 추출된 텍스트 없음")
                continue
            clipped = text[:file_store.CONTEXT_CHARS_PER_FILE]
            note = "" if len(clipped) == len(text) else f"\n(파일이 길어 앞쪽 {len(clipped):,}자만 포함 — 전체 {len(text):,}자)"
            parts.append(f"[출처: 업로드 파일 - {row['filename']} (사용자가 대화에 첨부한 파일, 링크 없음)]\n{clipped}{note}")
            sources.append({"type": "file", "label": row["filename"], "url": None, "file_id": fid})
            items.append(f"{row['filename']}: {len(clipped):,}자 반영")
        st.detail = f"{len(parts)}/{len(file_ids)}개 반영"
        st.items = items
    return parts, sources


def _execute_chat(prep, emit=None):
    """질문 하나를 끝까지 처리한다(검색 → 생성 → (비교 시) 채택 → 저장). 단계마다 tracer로
    이벤트를 남기고, 마지막에 run 기록을 저장한 뒤 final dict를 돌려준다."""
    tracer = Tracer(emit)
    history = prep["history"]
    conversation_id = prep["conversation_id"]
    run_id = prep["run_id"]
    question = history[-1]["content"]
    candidates = prep["candidates"]
    run = {
        "id": run_id, "conversation_id": conversation_id, "question": question,
        "mode": "compare" if candidates else "single", "status": "running",
        "created_at": chat_history._now(),
    }
    with _LIVE_RUNS_LOCK:
        _LIVE_RUNS.setdefault(run_id, {"conversation_id": conversation_id})["tracer"] = tracer
    try:
        chat_history.save_run(run)  # 시작 시점에 "running"으로 먼저 남겨 기록 화면에도 진행 중으로 보이게
    except Exception as e:
        print(f"⚠️  실행 기록 저장 실패: {e}", file=sys.stderr)
    tracer.note("receive", "질문 접수", "새 대화" if prep["is_new"] else ("같은 질문 재생성" if prep["retry"] else "이어지는 대화"))
    if not prep["retry"]:
        # 질문에도 run_id를 남겨, 실행 기록 1건 삭제 시 질문·답변 한 쌍을 정확히 숨길 수 있게 한다
        chat_history.add_message(conversation_id, "user", question, run_id=run_id)

    try:
        # 사용량 한도 초과 상태면 검색/답변 생성(둘 다 claude -p 필요)을 아예 시도하지
        # 않고 바로 고정 메시지로 응답한다(사용자 요청, 2026-09-18) — build_context도
        # 검색어 확장/인물 판별에 claude -p를 쓰므로 여기서 걸러야 검색 자체가
        # "비활성화"된다. usage_guard.py 모듈 독스트링 참고: 정확한 잔여 % 조회는
        # 불가능해서, 실제 실패를 감지한 뒤 쿨다운 동안만 이렇게 막는 반응형 방식이다.
        if is_usage_exhausted():
            tracer.note("usage", "사용량 한도 확인", "한도 소진 — 검색/생성 생략", status="error")
            answer, sources, adopted, cand_results, verdict = USAGE_EXHAUSTED_MESSAGE, [], None, [], None
        else:
            upload_parts, upload_sources = build_upload_context(prep["file_ids"], prep["user_id"], prep["anon_id"], tracer)
            search_query = _build_search_query(history)
            context, sources = build_context(search_query, history, tracer)
            if upload_parts:
                context = "\n\n---\n\n".join(upload_parts + ([context] if context else []))
                sources = upload_sources + sources

            if candidates:
                def _gen(model_id):
                    return generate_answer(history, context, model_id)[0]

                def _judge(prompt, system):
                    return _claude_cli_raw(prompt, system, model=JUDGE_MODEL, timeout=150)

                answer, adopted, cand_results, verdict = model_arena.run_arena(
                    question, context, candidates, _gen, _judge, tracer,
                    judge_model_label=model_arena.model_label(f"claude:{JUDGE_MODEL}"),
                )
                backend = "ollama" if adopted.startswith("ollama:") else "claude"
            else:
                model = prep["model"] or _default_model_id()
                label = model_arena.model_label(model)
                with tracer.step("generate", f"답변 생성 · {label}") as st:
                    tracer.push({"type": "candidate", "model": model, "label": label, "status": "running"})
                    started = time.monotonic()
                    answer, backend = generate_answer(history, context, model)
                    ms = int((time.monotonic() - started) * 1000)
                    tracer.push({"type": "candidate", "model": model, "label": label, "status": "done",
                                 "ms": ms, "chars": len(answer)})
                    st.detail = f"{len(answer):,}자"
                adopted = model
                cand_results = [{"model": model, "label": label, "status": "done", "ms": ms,
                                 "chars": len(answer), "decision": "채택"}]
                verdict = None
            if backend == "ollama":
                answer = _ensure_real_images_shown(answer, context)
            answer = _fix_image_paths(answer)

        with tracer.step("save", "대화·실행 기록 저장"):
            if prep["retry"]:
                chat_history.replace_last_message(conversation_id, "assistant", answer, sources, run_id=run_id)
            else:
                chat_history.add_message(conversation_id, "assistant", answer, sources, run_id=run_id)
        run.update(status="done", adopted_model=adopted, candidates=cand_results, verdict=verdict,
                   sources_count=len(sources))
        return {
            "type": "final", "answer": answer, "sources": sources, "conversation_id": conversation_id,
            "run_id": run_id, "adopted_model": adopted, "candidates": cand_results, "verdict": verdict,
        }
    except Exception as e:
        tracer.push({"type": "error", "error": str(e)[:500]})
        run.update(status="error")
        raise
    finally:
        run["events"] = tracer.events
        run["duration_ms"] = tracer.elapsed_ms()
        try:
            chat_history.save_run(run)
        except Exception as e:
            print(f"⚠️  실행 기록 저장 실패: {e}", file=sys.stderr)
        with _LIVE_RUNS_LOCK:
            _LIVE_RUNS.pop(run_id, None)


@app.route("/api/chat", methods=["POST"])
def chat():
    body = request.get_json(force=True) or {}
    user_id, anon_id = _identity()
    try:
        prep = _prepare_chat(body, user_id, anon_id)
        final = _execute_chat(prep)
    except _ChatError as e:
        return jsonify({"error": str(e)}), e.code
    final.pop("type", None)
    return jsonify(final)


@app.route("/api/chat/stream", methods=["POST"])
def chat_stream():
    """/api/chat과 같은 처리를 하되, 단계 이벤트를 NDJSON(한 줄에 JSON 하나)으로 흘려보낸다.
    처리는 별도 스레드에서 돌기 때문에 브라우저가 중간에 연결을 끊어도 끝까지 진행돼
    답변과 실행 기록이 저장된다(다시 열면 기록에서 확인 가능)."""
    body = request.get_json(force=True) or {}
    user_id, anon_id = _identity()
    try:
        prep = _prepare_chat(body, user_id, anon_id)
    except _ChatError as e:
        return jsonify({"error": str(e)}), e.code

    events = queue.Queue()

    def worker():
        try:
            events.put(_execute_chat(prep, emit=events.put))
        except Exception as e:
            print(f"⚠️  chat 처리 실패: {e}", file=sys.stderr)
            events.put({"type": "error", "error": str(e)[:500], "fatal": True})
        finally:
            events.put(None)

    threading.Thread(target=worker, daemon=True).start()

    def generate():
        yield json.dumps({
            "type": "start", "run_id": prep["run_id"], "conversation_id": prep["conversation_id"],
            "mode": "compare" if prep["candidates"] else "single", "candidates": prep["candidates"],
        }, ensure_ascii=False) + "\n"
        while True:
            try:
                ev = events.get(timeout=15)
            except queue.Empty:
                yield json.dumps({"type": "ping"}) + "\n"  # 긴 claude -p 호출 동안 연결 유지
                continue
            if ev is None:
                break
            yield json.dumps(ev, ensure_ascii=False) + "\n"

    return Response(generate(), mimetype="application/x-ndjson",
                    headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


def _run_owned(run, user_id, anon_id):
    conv = chat_history.get_conversation(run.get("conversation_id") or "", include_deleted=ADMIN_MODE)
    return conv is not None and _owned_by(conv, user_id, anon_id)


@app.route("/api/runs")
def runs_list():
    user_id, anon_id = _identity()
    rows = chat_history.list_runs(
        limit=min(int(request.args.get("limit", 200)), 500),
        user_id=user_id, anon_session_id=anon_id, include_deleted=ADMIN_MODE,
    )
    with _LIVE_RUNS_LOCK:
        live_ids = set(_LIVE_RUNS)
    for r in rows:  # 목록에는 후보 답변 원문까지는 필요 없음
        if r["status"] == "running" and r["id"] not in live_ids:
            r["status"] = "error"  # 서버 재시작 등으로 끝나지 못한 실행
        r["candidates"] = [
            {k: c.get(k) for k in ("model", "label", "status", "score", "decision", "ms")}
            for c in (r.get("candidates") or [])
        ]
    return jsonify({"runs": rows})


@app.route("/api/runs/<run_id>", methods=["DELETE"])
def run_delete(run_id):
    run = chat_history.get_run(run_id)
    user_id, anon_id = _identity()
    if run is None or not _run_owned(run, user_id, anon_id):
        return jsonify({"error": "실행 기록을 찾을 수 없습니다."}), 404
    with _LIVE_RUNS_LOCK:
        if run_id in _LIVE_RUNS:
            return jsonify({"error": "아직 답을 만드는 중인 기록은 지울 수 없습니다."}), 409
    conv_deleted = chat_history.delete_run(run_id)
    return jsonify({"ok": True, "conversation_deleted": conv_deleted})


@app.route("/api/runs/<run_id>")
def run_detail(run_id):
    run = chat_history.get_run(run_id)
    user_id, anon_id = _identity()
    if run is None or not _run_owned(run, user_id, anon_id):
        return jsonify({"error": "실행 기록을 찾을 수 없습니다."}), 404
    with _LIVE_RUNS_LOCK:
        live = _LIVE_RUNS.get(run_id)
        tracer = live and live.get("tracer")
        if tracer is not None:
            # 아직 만드는 중 — DB에는 시작 시점 기록뿐이라 메모리의 실시간 이벤트로 채운다
            run["events"] = list(tracer.events)
            run["duration_ms"] = tracer.elapsed_ms()
    run["live"] = tracer is not None
    if run["status"] == "running" and not run["live"]:
        run["status"] = "error"  # 서버 재시작 등으로 끝나지 못한 실행
    return jsonify(run)


def _file_meta(row):
    return {k: row[k] for k in ("id", "filename", "size", "kind", "text_chars", "created_at")}


@app.route("/api/files", methods=["GET"])
def files_list():
    user_id, anon_id = _identity()
    return jsonify({"files": [_file_meta(r) for r in chat_history.list_files(user_id=user_id, anon_session_id=anon_id)]})


@app.route("/api/files", methods=["POST"])
def files_upload():
    user_id, anon_id = _identity()
    uploaded = request.files.getlist("file")
    if not uploaded:
        return jsonify({"error": "업로드할 파일이 없습니다."}), 400
    saved, errors = [], []
    for f in uploaded:
        name = os.path.basename(f.filename or "").strip() or "upload.txt"
        try:
            data = f.read()
            file_id, stored_name, kind, text_chars = file_store.save_upload(name, data)
        except ValueError as e:
            errors.append(f"{name}: {e}")
            continue
        chat_history.create_file(file_id, name, len(data), kind, text_chars, stored_name,
                                 user_id=user_id, anon_session_id=anon_id)
        saved.append(_file_meta(chat_history.get_file(file_id)))
    status = 200 if saved else 400
    return jsonify({"files": saved, "errors": errors, "error": "; ".join(errors) if not saved else None}), status


def _owned_file_or_404(file_id):
    row = chat_history.get_file(file_id)
    user_id, anon_id = _identity()
    if row is None or not _owned_by(row, user_id, anon_id):
        return None
    return row


@app.route("/api/files/<file_id>", methods=["GET"])
def file_detail(file_id):
    row = _owned_file_or_404(file_id)
    if row is None:
        return jsonify({"error": "파일을 찾을 수 없습니다."}), 404
    meta = _file_meta(row)
    meta["preview"] = file_store.read_text(file_id)[:4000]
    return jsonify(meta)


@app.route("/api/files/<file_id>/download")
def file_download(file_id):
    row = _owned_file_or_404(file_id)
    if row is None:
        return jsonify({"error": "파일을 찾을 수 없습니다."}), 404
    return send_from_directory(file_store.UPLOAD_DIR, row["stored_name"], as_attachment=True,
                               download_name=row["filename"])


@app.route("/api/files/<file_id>", methods=["DELETE"])
def file_delete(file_id):
    row = _owned_file_or_404(file_id)
    if row is None:
        return jsonify({"error": "파일을 찾을 수 없습니다."}), 404
    file_store.remove(row)
    chat_history.delete_file(file_id)
    return jsonify({"ok": True})


@app.route("/api/conversations")
def conversations():
    if ADMIN_MODE:
        convs = chat_history.list_conversations(include_deleted=True)
    else:
        user_id, anon_id = _current_identity()
        convs = chat_history.list_conversations(user_id=user_id, anon_session_id=anon_id)
    return jsonify({"conversations": convs})


@app.route("/api/conversations/<conversation_id>")
def conversation_detail(conversation_id):
    conv = chat_history.get_conversation(conversation_id, include_deleted=ADMIN_MODE)
    if conv is None:
        return jsonify({"error": "대화를 찾을 수 없습니다."}), 404
    if not ADMIN_MODE and not _owns_conversation(conv):
        return jsonify({"error": "대화를 찾을 수 없습니다."}), 404
    conv["active_run_id"] = _live_run_for_conversation(conversation_id)
    return jsonify(conv)


@app.route("/api/conversations/<conversation_id>", methods=["DELETE"])
def conversation_delete(conversation_id):
    if not ADMIN_MODE:
        conv = chat_history.get_conversation(conversation_id, include_deleted=True)
        if conv is None or not _owns_conversation(conv):
            return jsonify({"error": "대화를 찾을 수 없습니다."}), 404
    chat_history.delete_conversation(conversation_id)
    return jsonify({"ok": True})


@app.route("/api/conversations/<conversation_id>/restore", methods=["POST"])
def conversation_restore(conversation_id):
    if not ADMIN_MODE:
        conv = chat_history.get_conversation(conversation_id, include_deleted=True)
        if conv is None or not _owns_conversation(conv):
            return jsonify({"error": "대화를 찾을 수 없습니다."}), 404
    chat_history.restore_conversation(conversation_id)
    return jsonify({"ok": True})


@app.route("/api/conversations/<conversation_id>/purge", methods=["DELETE"])
def conversation_purge(conversation_id):
    if not ADMIN_MODE:
        return jsonify({"error": "관리자 전용 기능입니다."}), 403
    chat_history.purge_conversation(conversation_id)
    return jsonify({"ok": True})


FEEDBACK_TO_ADDRESS = "hyunje.sung@qcells.com"
GMAIL_ADDRESS = os.environ.get("GMAIL_ADDRESS", "")
GMAIL_APP_PASSWORD = os.environ.get("GMAIL_APP_PASSWORD", "")


def send_feedback_email(subject, body_text):
    """Gmail SMTP(smtp.gmail.com:587, STARTTLS)로 메일을 보낸다. 발신 계정은 GMAIL_ADDRESS/
    GMAIL_APP_PASSWORD 환경변수로 지정하는 발신 전용 Gmail 계정(2단계 인증 후 발급한 앱 비밀번호
    사용 — 일반 로그인 비밀번호로는 SMTP 인증이 막힌다). 받는 주소는 FEEDBACK_TO_ADDRESS 고정.

    예전엔 Windows Outlook 데스크톱 앱을 COM으로 자동화(WSL2 전용 편법)했는데, 위키봇을 라즈베리
    파이(Linux)에도 올리면서 플랫폼에 무관하게 동작하는 방식이 필요해져 SMTP로 전환함(사용자 확정,
    2026-09-09) — 사내 Exchange는 순수 SMTP가 아니라 자체 프로토콜(MAPI/EWS)을 쓰고 내부 릴레이
    주소는 IT팀만 알아서, 그걸 기다리는 대신 별도 Gmail 계정을 발신 전용으로 씀."""
    if not GMAIL_ADDRESS or not GMAIL_APP_PASSWORD:
        return False, "GMAIL_ADDRESS/GMAIL_APP_PASSWORD 환경변수가 설정되지 않았습니다."

    html_body = (
        '<html><body><pre style="font-family:Consolas,\'Malgun Gothic\',monospace;'
        'font-size:13px;white-space:pre-wrap;">' + html.escape(body_text) + "</pre></body></html>"
    )
    msg = EmailMessage()
    msg["From"] = GMAIL_ADDRESS
    msg["To"] = FEEDBACK_TO_ADDRESS
    msg["Subject"] = subject
    msg.set_content(body_text)
    msg.add_alternative(html_body, subtype="html")

    try:
        with smtplib.SMTP("smtp.gmail.com", 587, timeout=20) as server:
            server.starttls(context=ssl.create_default_context())
            server.login(GMAIL_ADDRESS, GMAIL_APP_PASSWORD)
            server.send_message(msg)
    except Exception as e:
        return False, str(e)
    return True, ""


@app.route("/api/feedback", methods=["POST"])
def feedback():
    body = request.get_json(force=True) or {}
    author = (body.get("author") or "").strip() or "익명"
    message = (body.get("message") or "").strip()
    if not message:
        return jsonify({"error": "메시지를 입력해주세요."}), 400
    transcript = body.get("transcript") or []
    conversation_id = (body.get("conversation_id") or "").strip()

    submitted_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    # 리버스 프록시 없이 Flask에 직접 붙는 구성이라 X-Forwarded-For는 클라이언트가 위조 가능 —
    # 신뢰할 수 있는 remote_addr만 사용한다.
    client_ip = request.remote_addr or "알 수 없음"
    user_agent = request.headers.get("User-Agent", "알 수 없음")

    # transcript는 프론트에서 "수정요청" 버튼이 달린 답변까지만 잘라서 보낸다(appendMessageActions
    # 호출부 참고) — 즉 마지막 원소가 항상 그 버튼이 달린 답변이고, 그 바로 앞이 그 답변을 유발한
    # 질문이다. 이 두 턴을 "대상 질문/답변"으로 따로 뽑아 어떤 질문에서 제보됐는지 바로 보이게 한다.
    target_question = ""
    if transcript and (transcript[-1].get("role") or "") == "assistant":
        if len(transcript) >= 2 and (transcript[-2].get("role") or "") == "user":
            target_question = (transcript[-2].get("content") or "").strip()
    target_index = len(transcript) - 2  # 질문 턴의 인덱스(전체 대화 덤프에서 표시할 위치)

    info_lines = [f"작성자: {author}", f"시각: {submitted_at}", f"IP: {client_ip}", f"User-Agent: {user_agent}"]
    if target_question:
        info_lines.insert(1, f"대상 질문: {target_question}")
    parts = [
        "[제보 정보]\n" + "\n".join(info_lines),
        f"[제보 내용]\n{message}",
    ]
    turn_lines = []
    for i, turn in enumerate(transcript):
        role = turn.get("role")
        content = (turn.get("content") or "").strip()
        if not content:
            continue
        label = "질문" if role == "user" else "답변"
        marker = " ⬅ 수정요청 대상" if i in (target_index, target_index + 1) else ""
        turn_lines.append(f"[{label}]{marker}\n{content}")
    if turn_lines:
        parts.append("[대화 내용 전체]\n" + "\n\n".join(turn_lines))
    if conversation_id:
        parts.append(f"[대화 ID]\n{conversation_id}")
    body_text = "\n\n".join(parts)
    subject = f"[위키봇 수정요청] {author} - " + (message[:60] + ("…" if len(message) > 60 else ""))

    ok, err = send_feedback_email(subject, body_text)
    if not ok:
        return jsonify({"error": f"메일 전송 실패: {err}"}), 500
    return jsonify({"ok": True})


@app.route("/health")
def health():
    return jsonify({"status": "ok"})


@app.route("/api/whoami")
def whoami():
    logged_in = bool(session.get("user_id")) if not ADMIN_MODE else False
    return jsonify({
        "admin": bool(ADMIN_MODE),
        "version": WIKIBOT_VERSION,
        "logged_in": logged_in,
        "username": session.get("username") if logged_in else None,
    })


def main():
    port = 8010
    if "--port" in sys.argv:
        port = int(sys.argv[sys.argv.index("--port") + 1])
    if ADMIN_MODE:
        if not ADMIN_USERNAME or not ADMIN_PASSWORD_HASH:
            print("[admin] 경고: ADMIN_USERNAME/ADMIN_PASSWORD_HASH가 설정되지 않아 아무도 로그인할 수 없습니다.")
        print(f"🔐 Qcells EMS 위키봇 (관리자 모드) 서버 시작 → http://localhost:{port}")
    else:
        print(f"🤖 Qcells EMS 위키봇 서버 시작 → http://localhost:{port}")
    app.run(host="0.0.0.0", port=port, debug=False)


if __name__ == "__main__":
    main()
