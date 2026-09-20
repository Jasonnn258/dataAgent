"""Seed the deterministic fixture git repo used by Phase 3/4/6 tests,
rollback tasks and evaluation.

Builds experiments/fixtures/fixture_repo — a miniature Next.js-style TS/TSX
app whose git history contains the canonical rollback scenario:

  c1 init        scaffold: layout, home, login page, auth lib, api routes
  c2 feat        add retry wrapper to generation (ai-helpers only)
  c3 chore       MIXED commit: reword system title (layout.tsx) *and* change
                 login validation (auth.ts + login page copy)  <-- rollback target
  c4 docs        README only

Gold for rollback on c3 is known *by construction*:
  unit-title    = layout.tsx hunks            (keep when query says 保留标题)
  unit-auth     = auth.ts + login/page.tsx    (rollback when query says 登录改坏了)

Deterministic: fixed authors/dates/messages. Re-running rebuilds from scratch.
Usage:  python experiments/fixtures/seed_fixture.py [--force]
"""
from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
FIXTURE = ROOT / "fixture_repo"

FIXTURE_ENV = {
    **os.environ,
    "GIT_AUTHOR_NAME": "fixture", "GIT_AUTHOR_EMAIL": "fixture@example.com",
    "GIT_COMMITTER_NAME": "fixture", "GIT_COMMITTER_EMAIL": "fixture@example.com",
    "GIT_AUTHOR_DATE": "2026-09-18T10:00:00 +0800",
    "GIT_COMMITTER_DATE": "2026-09-18T10:00:00 +0800",
}

# ---------------------------------------------------------------- file versions
LAYOUT_V1 = '''import type { Metadata } from "next";
import { AuthNav } from "@/components/AuthNav";
import "./globals.css";

export const metadata: Metadata = {
  title: "SUN 教学设计系统",
  description: "教学设计生成工具",
};

export default function RootLayout({ children }: { children: React.ReactNode }) {
  return (
    <html lang="zh">
      <body>
        <AuthNav />
        <main>{children}</main>
      </body>
    </html>
  );
}
'''

LAYOUT_V2 = LAYOUT_V1.replace('title: "SUN 教学设计系统"', 'title: "SUN 思维太阳教学设计系统 - 三图六构"')

AUTH_V1 = '''import { db } from "@/lib/db";

export async function hashPassword(password: string): Promise<string> {
  const data = new TextEncoder().encode(password);
  const hash = await crypto.subtle.digest("SHA-256", data);
  return Array.from(new Uint8Array(hash)).map(b => b.toString(16).padStart(2, "0")).join("");
}

export async function verifyPassword(password: string, hash: string): Promise<boolean> {
  const candidate = await hashPassword(password);
  return candidate === hash;
}

export function validateAccount(account: string): boolean {
  return account.length >= 4;
}
'''

# c3 introduces a *regression*: min length 8 instead of 4
AUTH_V2 = AUTH_V1.replace("return account.length >= 4;", "return account.length >= 8;")

LOGIN_PAGE_V1 = '''"use client";
import { useAuth } from "@/contexts/AuthContext";
import { Button } from "@/components/ui/button";

export default function LoginPage() {
  const { login } = useAuth();
  const handleLogin = async (account: string, password: string) => {
    const result = await login(account, password);
    if (!result.success) {
      console.error("登录失败", result.error);
    }
  };
  return (
    <form>
      <h1>用户登录</h1>
      <Button onClick={() => handleLogin("user1", "x")}>登录</Button>
    </form>
  );
}
'''

LOGIN_PAGE_V2 = LOGIN_PAGE_V1.replace('"登录失败", result.error', '"登录失败，请检查账号格式", result.error')

AUTHNAV = '''"use client";
import { useAuth } from "@/contexts/AuthContext";

export function AuthNav() {
  const { user, logout } = useAuth();
  if (!user) return null;
  return (
    <nav>
      <span>{user.name}</span>
      <button onClick={logout}>退出登录</button>
    </nav>
  );
}
'''

AUTH_CONTEXT = '''"use client";
import { useRouter } from "next/navigation";

export function useAuth() {
  const router = useRouter();
  const login = async (account: string, password: string) => {
    const res = await fetch("/api/auth/login", { method: "POST", body: JSON.stringify({ account, password }) });
    if (res.ok) { router.refresh(); return { success: true }; }
    return { success: false, error: "bad credentials" };
  };
  const logout = async () => { await fetch("/api/auth/logout", { method: "POST" }); };
  return { user: null, login, logout };
}
'''

HOME = '''import { AuthNav } from "@/components/AuthNav";

export default function Page() {
  return (
    <div>
      <h1>首页</h1>
      <p>教学设计生成工具</p>
    </div>
  );
}
'''

AI_HELPERS_V1 = '''import { generateWithRetry } from "./retry";

export async function generateDesign(prompt: string): Promise<string> {
  const out = await generateWithRetry(prompt, 2);
  return out;
}
'''

AI_HELPERS_V2 = '''import { generateWithRetry } from "./retry";

export async function generateDesign(prompt: string): Promise<string> {
  const out = await generateWithRetry(prompt, 3);
  return out;
}

export async function generateReview(prompt: string): Promise<string> {
  return generateWithRetry(prompt, 3);
}
'''

RETRY = '''export async function generateWithRetry(prompt: string, retries: number): Promise<string> {
  for (let i = 0; i < retries; i++) {
    try {
      const res = await fetch("/api/generate", { method: "POST", body: prompt });
      if (res.ok) return await res.text();
    } catch (e) {
      if (i === retries - 1) throw e;
    }
  }
  throw new Error("generate failed after retries");
}
'''

API_LOGIN = '''import { NextRequest, NextResponse } from "next/server";
import { verifyPassword, validateAccount } from "@/lib/auth";

export async function POST(req: NextRequest) {
  const { account, password } = await req.json();
  if (!validateAccount(account)) {
    return NextResponse.json({ error: "invalid account" }, { status: 400 });
  }
  const ok = await verifyPassword(password, "stored-hash");
  if (!ok) return NextResponse.json({ error: "wrong password" }, { status: 401 });
  return NextResponse.json({ ok: true });
}
'''

API_GENERATE = '''import { NextRequest, NextResponse } from "next/server";
import { generateDesign } from "@/lib/ai-helpers";

export async function POST(req: NextRequest) {
  const prompt = await req.text();
  const design = await generateDesign(prompt);
  return NextResponse.json({ design });
}
'''

DB = '''export const db = {
  user: { findFirst: async (_q: unknown) => null },
};
'''


def w(rel: str, content: str) -> None:
    p = FIXTURE / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(content, encoding="utf-8")


def git(*args: str, date: str) -> str:
    env = {**FIXTURE_ENV, "GIT_AUTHOR_DATE": date, "GIT_COMMITTER_DATE": date}
    proc = subprocess.run(["git", "-C", str(FIXTURE), *args], env=env,
                          capture_output=True, text=True)
    if proc.returncode != 0:
        raise RuntimeError(f"git {args} failed: {proc.stderr}")
    return proc.stdout.strip()


def seed(force: bool = False) -> Path:
    if FIXTURE.exists():
        if not force:
            print(f"fixture exists at {FIXTURE} (use --force to rebuild)")
            return FIXTURE
        shutil.rmtree(FIXTURE)
    FIXTURE.mkdir(parents=True)
    git("init", "-q", date="2026-09-18T09:00:00 +0800")

    # c1 — scaffold
    w("src/app/layout.tsx", LAYOUT_V1)
    w("src/app/page.tsx", HOME)
    w("src/app/login/page.tsx", LOGIN_PAGE_V1)
    w("src/components/AuthNav.tsx", AUTHNAV)
    w("src/contexts/AuthContext.tsx", AUTH_CONTEXT)
    w("src/lib/auth.ts", AUTH_V1)
    w("src/lib/db.ts", DB)
    w("src/lib/ai-helpers.ts", AI_HELPERS_V1)
    w("src/lib/retry.ts", RETRY)
    w("src/app/api/auth/login/route.ts", API_LOGIN)
    w("src/app/api/generate/route.ts", API_GENERATE)
    git("add", "-A", date="2026-09-18T09:01:00 +0800")
    git("commit", "-qm", "init: scaffold teaching design app (layout, auth, api)",
        date="2026-09-18T09:01:00 +0800")

    # c2 — generation retry only
    w("src/lib/ai-helpers.ts", AI_HELPERS_V2)
    git("add", "-A", date="2026-09-18T09:02:00 +0800")
    git("commit", "-qm", "feat: raise generate retry budget and add review generation",
        date="2026-09-18T09:02:00 +0800")

    # c3 — MIXED: title reword (keep-worthy) + login validation regression (bug)
    w("src/app/layout.tsx", LAYOUT_V2)
    w("src/lib/auth.ts", AUTH_V2)
    w("src/app/login/page.tsx", LOGIN_PAGE_V2)
    git("add", "-A", date="2026-09-18T09:03:00 +0800")
    git("commit", "-qm", "chore: reword system title and tighten login account validation",
        date="2026-09-18T09:03:00 +0800")

    # c4 — docs only
    w("README.md", "# fixture repo\n\nDeterministic fixture for rollback/change-unit experiments.\n")
    git("add", "-A", date="2026-09-18T09:04:00 +0800")
    git("commit", "-qm", "docs: add readme", date="2026-09-18T09:04:00 +0800")

    print(f"fixture seeded at {FIXTURE}")
    for line in git("log", "--oneline", "--reverse", date="2026-09-18T09:04:00 +0800").splitlines():
        print(" ", line)
    return FIXTURE


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--force", action="store_true")
    args = ap.parse_args()
    seed(force=args.force)
