# 农产品残留限量版本与复检裁决

面向农产品检测站复核员，处理采信、判定、核算、复核与归档。

## 启动

```bash
docker compose up --build -d
```

访问 http://localhost:18254 。初次启动自动运行脏样例，可切换 clean.csv 与 legacy.csv。顶部“运行批次”会重新计算，同源输入复用批次。

## 业务模块

- 报告采信：`/m1_ingest`、`/api/m1_ingest`
- 项目映射：`/m2_mapping`、`/api/m2_mapping`
- 限量选版：`/m3_versions`、`/api/m3_versions`
- 检出量换算：`/m4_segments`、`/api/m4_segments`
- 复检裁决：`/m5_calculate`、`/api/m5_calculate`
- 批次判级：`/m6_reconcile`、`/api/m6_reconcile`
- 报告归档：`/m7_archive`、`/api/m7_archive`
- 定向补正：`/m8_correction`、`/api/m8_correction/*`

## 定向补正（隔离报告补正重放）

适用场景：首批报告中有报告因缺检出限等原因被隔离，且有复检 `relation` 指向其中一行；补正只发来该行报告。系统不做整批重跑（避免改换已签合格结论），也不只补单行（避免依赖它的复检悬空），而是执行**闭包定向重放**：

1. 以已确认批次的原始字节为基线，仅对补正目标的物理记录做字节级替换，其余字节、行号、空行、重复表头保持原样；
2. 重放闭包 = 补正目标 + 所有沿 `relation` 直接或间接依赖它的复检；
3. 闭包之外的采纳行、隔离原因、计算证据与对账必须与已确认批次逐项一致，否则判 `SCOPE` 越界，整笔补正失败、旧版结果继续有效（回滚）；
4. 身份/关联字段（样本、批次、产品、项目、方法、版本、采样/报告日、relation）冻结，只允许修复 `result/unit/lod/note`；
5. 成功后产生新 `run_id` 的批次结果，旧批次与全部隔离原因、补正台账完整留档；项目结论、批次判级、归档只采用同一份补正后证据；
6. 同一基线对同一目标的同一补正内容幂等，再次重跑不新增项目、不改换已确认范围。

接口：`POST /api/m8_correction/preview`（预演闭包与结论变化，不落盘）、`POST /api/m8_correction/apply`（提交并原子切换 latest）、`GET /api/m8_correction/targets`（可补正的隔离报告）。场景样例 `sample_data/correction.csv`：`00010`、`00020` 缺 LOD 被隔离，复检 `00011` 引用 `00010` 而悬空。

## 核心规则

按采样日期选择有效限量，低于检出限保留区间；复检不得越过样本关联与方法适用范围。完整数值、边界和异常定义见 `docs/DATA-DICTIONARY.md`。

## 验证

```bash
docker compose exec -T app pytest -q
docker compose config --quiet
```

原始样例在 `sample_data/`。测试在 `tests/`。封存文件是单份CSV，含运行清单、计算证据、隔离原因与对账。复核需提交当前版本，陈旧复核返回409。

## 数据与限制

SQLite保存在records命名卷，普通重启与构建不会清除。确认可丢弃本项目数据后，运行 `docker compose down -v` 再启动可重置。单文件上限2MB；不接入外部业务系统；历史规则由本项目样例定义，不代表实际行业标准。
