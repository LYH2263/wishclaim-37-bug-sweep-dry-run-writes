# Wishclaim · 礼物愿望认领

发布 → 认领锁定（互斥+TTL）→ 核销/释放。过期锁由「扫尾」干跑/提交两阶段释放，提交追加释放台账。

| 服务 | 端口 |
| --- | --- |
| 前端 | 5200 |
| API | 10200 |

```bash
docker compose up --build
pytest backend/app/tests
```

0-1：`wish_comment` / `secret_santa` / `price_cap`。

## 过期扫尾

| 方法/路径 | 说明 |
| --- | --- |
| `GET /api/sweep/dry-run` | 干跑：只读，返回将释放的 `wish_id`/`claimer`，不改 wishes、不写台账 |
| `POST /api/sweep/commit` | 提交：释放过期锁并追加一个台账批次（`batch_id`），重复提交不双增 |
| `GET /api/ledger` | 台账（按批次分组：wish_id、原 claimer、释放时刻） |
| `GET /api/wishes/{id}/events` | 详情事件流，每条 TTL 释放钉住 `batch_id` |

模块拆分：`app/modules/expiry_sweep`（扫尾服务）、`release_ledger`（台账）、
`sweep_projection`（墙卡可认领/详情事件/台账页投影）。干跑与提交共用同一候选口径
（`status='claimed'` 且 `expires_at<=now`）；open、未过期、已手动 `released` 的行不记账。
台账 `UNIQUE(wish_id, claimed_at)` 保证同一过期锁重复提交不双增，重新认领后再次过期另记一行。

