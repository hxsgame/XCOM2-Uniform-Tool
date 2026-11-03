# XCOM 2 Uniform Tool

XCOM 2: War of the Chosen 的离线工具：直接读写角色池 `DefaultCharacterPool.bin` 里
**Iridar's Appearance Manager（AM）** 的 `ExtraDatas`，查看/修改 Uniform（"非士兵(敌人)" 外观）
绑定了哪些敌人模板、以及它的 `UniformStatus`——不用进游戏，也不依赖任何 mod。

配套一个本地实时编辑页（`serve`）：卡片式浏览所有 Uniform、编辑绑定的敌人模板、上传预览图、
给 Uniform 打分；保存即写盘，写前自动备份并做不变量校验。

## 前置条件

- Windows，Python 3.8+（只用标准库，无第三方依赖）
- 已安装 WOTC 与 AM（否则角色池里没有 AM 的 `ExtraDatas`，工具只会提示没有可编辑数据）
- 修改前**完全退出游戏**：工具检测到 `XCom2.exe` 在运行会直接拒绝写入

## 用法

```bash
python uniform_tool.py list                    # 列出 Uniform 及绑定的模板
python uniform_tool.py report --out 总览.html  # 生成静态 HTML 总览
python uniform_tool.py serve                   # 本地实时编辑页（默认 8765 端口）
python uniform_tool.py bind   --uniform 机械忍者-黑 --template CerberusNemesisM2
python uniform_tool.py unbind --uniform 机械忍者-黑 --template CerberusNemesisM2
python uniform_tool.py status --uniform 机械忍者-黑 --value NonSoldier
python uniform_tool.py templates --search cerberus   # 在已安装 mod 里查模板来源
python uniform_tool.py corehash               # 校验写盘核心代码未被改动
python uniform_tool.py selftest               # 只读自检：解析 + 内存补丁 + 不变量
```

常用参数：

| 参数 | 说明 |
|---|---|
| `--pool` | 指定角色池 `.bin`，默认用 `%USERPROFILE%\Documents\My Games\XCOM2 War of the Chosen\XComGame\CharacterPool\DefaultCharacterPool.bin` |
| `--mods` | 指定 workshop 的 `content\268500` 目录（默认自动探测常见 Steam 库路径） |
| `--port` / `--no-open` | `serve` 的端口 / 不自动开浏览器 |
| `--gender` | 预览页按性别筛选（`both` / `male` / `female`） |
| `--set-status` | 允许把非 `NonSoldier` 的 Uniform 改成 `NonSoldier` |

## 安全设计

写盘逻辑被刻意做得很小，且三重兜底：

1. **不变量校验**：每次写入前后重新解析角色池，要求只有
   `NonSoldierUniformTemplates` 与 `UniformStatus` 两个字段发生变化；条目数、条目位置、
   文件头与文件尾必须逐字节不变。任一不满足就不落盘。
2. **写盘核心哈希锁**：二进制补丁逻辑的两段代码 SHA-256 记在 `write_core.lock.json`；
   哈希不符时 `write_pool` 拒绝写入，防止手滑改坏序列化代码。有意修改并人工复核后，
   运行 `python uniform_tool.py corehash --update` 更新锁。
3. **原子写 + 备份**：先写临时文件再 `os.replace`；`backups/` 下保留第一次编辑前的
   `<池名>.original.bin`（永久）与最近 10 份带时间戳的滚动备份。

另外，游戏运行中不写：游戏退出时会把内存里的角色池覆写回硬盘，此时写入会被覆盖。

## 历史

早期曾尝试做一个游戏内 mod（由 AM 的 API 在游戏里应用绑定）。实测结论：AM 的运行时写入
最终也是 `SaveCharacterPool()` 写回**同一个** `.bin`，安全上不比本工具多任何东西，只多出
"游戏运行中改内存"的便利；而编译它需要把 AM 完整源码与 Community Highlander 一起纳入
构建。因此该方案已废弃，改走"关游戏 → 本工具写文件 → 开游戏"。

## 说明

- 第三方工具，与 Firaxis、Iridar 无关；改的是你自己的存档数据，请留意 `backups/`。
- 角色池里不含外观图片，预览图需自行上传（仅存本地 `uniform_images/`）。
