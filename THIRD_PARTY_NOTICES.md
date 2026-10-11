# Third-party notices

CC Msg is MIT licensed. Parts of it are ported from, or follow the design of, these MIT-licensed projects.
Their license texts are in `licenses/`.

| Project | License | What CC Msg uses |
|---|---|---|
| [cc-switch](https://github.com/farion1231/cc-switch) — Copyright (c) 2025 Jason Young | MIT (`licenses/cc-switch-MIT.txt`) | Seed model price table (`agent_bridge/usage_prices.json`); the cache-normalized "fresh input" rule used when importing CC Switch history (`sql_helpers::fresh_input_sql`); the usage page layout; session-management ideas; provider presets and the read-only current-provider lookup on the 模型与 API page; the layout of its `providers.settings_config` (Claude `env`, Codex `auth` + `config`) that the read-only provider import parses (no code copied). |
| [magpie](https://github.com/yetone/magpie) — Copyright (c) 2026 yetone | MIT (`licenses/magpie-MIT.txt`) | Session-log usage dedup rules (Claude `message.id` + `requestId` with superseding revisions; Codex cumulative / per-turn `token_count` with reset segments, archive dedup), re-implemented in `agent_bridge/usage.py`. |
| [opencodex](https://github.com/lidge-jun/opencodex) — Copyright (c) 2026 opencodex contributors | MIT (`licenses/opencodex-MIT.txt`) | Proxy detection (default port 10100) and the idea of showing prices as display-time estimates; reading its `config.json` (providers / routes / subagentModels) and the `ocx-*` agent `<!-- ocx-route: … -->` marker to show each agent's real model. |
| [bloub](https://github.com/helefee/bloub) — Copyright (c) 2026 Jérémy Perret | MIT (`licenses/bloub-MIT.txt`) | Session avatars. |
