# 家庭智能规则与隐私

三代同堂家庭的**本地**设备编排服务：门锁、摄像头、空调、照明与睡眠设备在
同一时刻产生竞争动作时，系统按 **安全级别 → 授权范围 → 规则确立时间** 给出
唯一且可解释的决定；断网时云端规则失效但门锁等保底能力仍受本地控制；
摄像与健康数据只能为获准用途读取，撤回同意后停止新的使用。

## 快速开始

```bash
python3 service.py --check          # 基础配置自检
python3 service.py --port 8000      # 启动（首次自动装配种子场景到 ./data）
curl http://127.0.0.1:8000/health
npm test                            # HTTP 契约 + 83 项领域测试
```

服务首次启动把设备能力、成员、同意、规则版本、访客凭证全部落盘到本地；
之后重启从本地恢复，断网可独立运行。

## 核心保证

| 需求 | 实现 |
| --- | --- |
| 本地保存设备能力/同意/规则/凭证 | 全部状态经 `storage.LocalStore` 原子 JSON 落盘（临时文件 + rename） |
| 多动作竞争给唯一决定 | `engine._arbitrate` 按设备分组，`(安全级别, 授权范围, 确立时间, id)` 全序排序，每台设备唯一获胜，其余记 `suppressed` |
| 可解释 | 每个候选记录通过/拒绝/抑制原因码、名次；`GET /api/decisions/{id}` 返回解释投影 |
| 断网不锁死门锁 | 云端缺省授权离线即拒；`local_always` 能力（门锁键盘开锁、紧急疏散）走本地通道；设备离线时远程通道仍拒绝 |
| 固件升级/传感器缺失/离线 | `devices.DeviceManager` 显式状态机：`normal→updating→recovering→normal/degraded→offline`，升级失败回滚，重连必须重验能力 |
| 重复事件不再开锁/断电 | 去重指纹（`dedup_key` > 稳定事件 ID > 事件内容签名），重复事件回放原决定，不再次执行，跨重启有效 |
| 摄像/健康用途绑定 | 同意按 `数据类型 + 用途` 授予；规则必须声明 `purpose`/`data_use`；撤回立即生效，区分 `consent_missing` 与 `consent_withdrawn` |
| 访客临时通行 | 凭证含生效/失效时间、设备与命令范围、接待主人；过期/撤销/越权拒绝；离线仍可校验；成功使用计数 |
| 规则修改留版本 | 任何内容变更产生新版本，旧版本置 `superseded` 永久保留；执行记录引用 `rule_id:version`，可按当时版本复盘 |
| 只查自己有权知道的结果 | 可见集合 = 规则请求人 + 行为人（访客→接待主人）+ 数据主体；紧急事件全户可见；无权与不存在都返回 404 |
| 不能由记录推断他人活动 | 他人候选只暴露 `safety/scope/rank/outcome` 与泛化原因 `rule_not_admitted`，设备、命令、用途、具体拒绝原因全部脱敏 |

紧急动作（燃气泄漏开门疏散）在**传感器缺失**时按故障安全（fail-safe）放行并在
`notes` 留痕；但固件升级中、设备真正离线时不豁免。

## 模块结构

```
domain.py       领域对象与枚举（设备/能力/成员/同意/规则版本/访客凭证/事件/决定）
clock_utils.py  本地时间与跨零点时间窗
conditions.py   规则条件三态求值：true / false / unknown（传感器缺失）
consent.py      同意授予、用途校验、撤回（missing 与 withdrawn 区分）
rules.py        规则版本库：修改即新版本，禁用同样留痕
access.py       访客凭证有效期与范围校验
devices.py      设备降级/升级/离线状态机与执行前准入
engine.py       候选生成 → 条件/授权/同意/准入 → 仲裁 → 执行 → 幂等
audit.py        可见性集合与最小化解释投影
storage.py      原子本地 JSON 存储
household.py    聚合服务 + 三代同堂种子场景（8 条规则、5 名成员、6 台设备）
api.py          HTTP API
service.py      运行入口（保持 /health 契约不变）
tests/          83 项测试（仲裁/离线/幂等/降级/同意/访客/版本/审计/落盘/HTTP）
```

## HTTP API（身份头 `X-Member-Id`）

```
POST /api/events                        上报事件，返回仲裁结果（含按身份投影的解释）
GET  /api/decisions?as=mom              仅列出自己有权知道的决定
GET  /api/decisions/{id}                单个决定的可解释投影（无权=404）
GET  /api/rules[/...]                    规则与版本历史（非请求人/非长辈不可见）
POST /api/rules/{id}/update|disable     修改规则（自动产生新版本，需长辈/父母）
POST /api/consents                      授予同意（父母可代孩子）
POST /api/consents/{id}/withdraw        撤回同意（立即停止新使用）
POST /api/visitor-passes[/...]          访客凭证签发/撤销
GET  /api/devices                       设备状态清单
POST /api/devices/{id}/firmware/*       升级开始/完成/失败回滚
POST /api/devices/{id}/verify           恢复后能力重验
POST /api/devices/{id}/sensors/*        传感器丢失/恢复
POST /api/devices/{id}/heartbeat[-lost] 心跳/离线
POST /api/cloud                         切换联网状态（断网演练）
```

## 仲裁示例（夜间 23:05 走廊有人）

| 候选 | 请求人 | 安全级别 | 授权范围 | 结果 |
| --- | --- | --- | --- | --- |
| 爷爷起夜夜灯 → 走廊灯 `on` | grandpa | safety | explicit | **执行（第 0 名）** |
| 妈妈睡眠时段 → 走廊灯 `off` | mom | comfort | role | 抑制：`lost_conflict` |

妈妈查询该决定时能看到自己关灯动作被抑制的完整原因，但只看到获胜动作的
`safety=safety, scope=explicit`，看不到是谁、在哪台设备上的动作；孩子查询返回 404。

## 设计边界

- 身份模型是本地家庭部署的简化版（固定成员 + `X-Member-Id`），生产部署应替换为
  本地认证（如设备证书/配对码）；授权、同意、审计语义不依赖该简化。
- `RecordingExecutor` 只记录动作；对接真实设备驱动时实现
  `engine.ActionExecutor.execute` 即可。
