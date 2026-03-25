[English](README.md) | [繁體中文](README.zh.md)

# skill-proxy

發現並呼叫未載入系統提示的冷技能。

## 說明

Skill Proxy 解決大型技能庫的 token 預算問題。約 80% 的技能屬於「冷技能」，其描述已被移除以節省 context token。當使用者需求無法匹配任何熱技能時，Skill Proxy 會在預建的觸發詞索引中搜尋最佳匹配，並透明地呼叫對應技能，同時提供冷熱技能狀態管理功能。

## 功能

- 在 81+ 已安裝技能中進行模糊觸發詞索引搜尋
- 信心評分匹配（高 / 中 / 低三段閾值）
- 透過 Skill 工具無縫呼叫冷技能
- 冷熱狀態管理：apply、restore、status 指令
- 支援單一技能還原（適用於編輯或發布工作流）
- 新增技能後重建搜尋索引

## 使用方式

```
/skill-proxy
```

觸發語句：
- 「有沒有處理 X 的 skill？」
- 任何無法匹配當前熱技能的使用者需求
- 「你有哪些關於圖表的 skill？」

## 運作原理

Proxy 對 `~/.claude/data/skill-index/triggers.json` 索引執行 `match_skill.py`，返回信心評分前 3 名的候選技能。評分 >= 8 直接呼叫；4–7 則向使用者確認；低於 4 則回報無強匹配。被匹配的冷技能 `SKILL.md` 仍存在於磁碟，因此 Skill 工具可正常呼叫，無需將其描述載入系統提示。

## 需求

- Claude Code CLI
- `~/.claude/data/skill-index/triggers.json`（由 `build-triggers.py` 建立）
- `~/.local/bin/python3`（uv 管理的 Python 3.12）

## 授權

MIT
