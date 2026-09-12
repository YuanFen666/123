# chaoxing 超星网课自动化（题库链增强版）

基于开源项目 **[Samueli924/chaoxing](https://github.com/Samueli924/chaoxing)** v3.1.4 改造的版本，
核心是把「查题答题」做成一条**多级题库链**，并修掉了原版几个会实际影响刷课效果的问题。

> 详细排查与实测记录见 **[说明文档.md](说明文档.md)**。

---

## 一、相比原版做了什么

### 1. 三级题库链（核心）

```ini
provider = AI,TikuAnevol
```

按顺序询问，**第一个给出有效答案的胜出，后面的不再请求**；前一级没答案就自动落到下一级：

| 级别 | 题库 | 特点 | 实测耗时 |
|---|---|---|---|
| 1 | `TikuIcodef` 网课小工具（GO题） | 免费，通用题库 | 0.2 ~ 1.6 秒/题 |
| 2 | `TikuAnevol` ANEVOL | 需 token，覆盖专业题 | 2.7 ~ 13 秒/题 |
| 3 | `AI` 任意 OpenAI 兼容大模型 | 前两级都没答案时才调用 | 1.6 秒/题 |

实测：`icodef → ANEVOL` 组合比「只用 ANEVOL」快 **10~20 倍**；
加上 AI 兜底后，一批 123 道题**全部拿到了答案，0 道落到随机作答**。

顺序可以在 `config.ini` 里任意调整，也可以只用其中一个（写单个类名即可）。

### 2. 网课小工具题库的「1 并发」限制

实测并发请求会被限流：

```json
{"code":-2,"data":"触发流控限制: 1并发限制"}
```

而 chaoxing 默认 `jobs = 8`。已用**类级锁把请求整体串行化**（锁一直握到拿到响应为止），
并把请求间隔默认调到 1.5 秒以摊平突发。自测里用 6 线程并发验证，实际并发数恒为 1。

### 3. 答案归一化（原版会答错的地方）

不同题库返回的答案形式完全不同：

- ANEVOL 返回**选项字母**（`"B"`）—— 原样交回 chaoxing 会被 `clean_res()` 删成空串，
  导致 `is_subsequence("", 选项)` 恒为真，**永远选中第一个选项**；
- 网课小工具返回**选项原文**（`"《诗经》"`），多选题用 `#` 连接；
- 大模型可能返回原文、字母，甚至把 `Answer` 写成字符串而不是列表。

已抽出公共的 `_AnswerNormalizeMixin` 统一处理：字母 ↔ 选项原文互转、单选净化
（去掉会被 `check_single` 当成多段答案的顿号/逗号）、判断题归一化（正确/错误/√/×）。

### 4. 熔断与限流

| 组件 | 策略 |
|---|---|
| ANEVOL | 连续 5 次服务故障熔断 120s（逐级翻倍至 900s）；额度/付费类错误直接停 600s |
| 网课小工具 | 触发流控退避重试；连续 5 题失败熔断 60s（逐级翻倍至 300s） |
| AI 兜底 | 请求间隔加锁；连续 5 次失败熔断 300s；鉴权/余额类错误停 600s |

**「题不在题库里」不计入熔断** —— 否则一片冷门题就会把整个题库停掉几分钟。

### 5. 日志分级：没查到 ≠ 调用失败

原版把「某个题库没答上来」记成 `ERROR ... 获取答案失败`，在多级题库链下极具误导性
（后面早就答出来了）。现在：

| 情况 | 级别 |
|---|---|
| 题库没收录该题 | INFO |
| 链上某一环没答案（中间过程） | DEBUG |
| 链上某一环服务故障 | WARNING |
| 链路最终命中 | INFO（`题库链命中：ANEVOL题库`） |
| **链路全部失守**（唯一该看的 ERROR） | ERROR |
| 额度/鉴权问题 | ERROR |

### 6. 原版 bug 修复

- **worker 线程自愈**：原版 `log_error` 装饰器会把异常直接掀掉整个 worker 线程，
  表现是「越跑越慢，最后只能同时看两条视频」。已改为异常转 `ChapterResult.ERROR` 走重试 + 看门狗补位。
- **题号预处理**：原版用 `^\d+` 去题号，会把 `"1+1等于几？"` 削成 `"+1等于几？"`、
  `"1921年..."` 削成 `"年..."`，题干变形直接搜不到。已改为「数字后必须跟标点且不超过 3 位」。
- **字体表打包**：`resource/font_map_table.json` 必须显式打进 exe，否则题干乱码。
- **带 BOM 的配置文件**：Windows 记事本「另存为 UTF-8」会给文件加 BOM，而 configparser
  用 `utf8` 读带 BOM 的文件会抛 `MissingSectionHeaderError`（第一段变成 `\ufeff[common]`），
  现象是**程序一启动就崩**。已全部改用 `utf-8-sig` 读取（带不带 BOM 都能读）。
- **并发写答案缓存**：`Tiku.query` 每次查询都会 `new` 一个 `CacheDAO()`，而原版把锁挂在**实例**上
  （`self._lock = threading.RLock()`），等于每个 worker 各持一把锁 = 没锁。8 个 worker 并发答题时
  多个线程同时 `os.replace()` 抢写 `cache.json`，Windows 上报
  `[WinError 5] 拒绝访问`（实测日志刷了 10 次），答案是写不进缓存、后面被重复查询。
  已改为**模块级共享锁** + `os.replace` 退避重试，并新增 `selftest_cache.py` 回归测试。
  > 这个 bug 一直存在，只是原版题库命中率低、几乎不写缓存，所以从没暴露；
  > 接了题库链之后命中率接近 100%，缓存写入频率暴增才把它打出来。

### 7. 考试：看板 / 就绪体检 / 进入考场自动答题

启动脚本的考试模式有三档：

| 选项 | 行为 | 命令 |
|---|---|---|
| 1 | 只看考试情况（**只读**，不进考场） | `--exam-only` |
| 2 | **进考场自动答题，但不交卷**（推荐第一次用） | `--exam-only --exam-take` |
| 3 | 进考场自动答题 **并自动交卷** | `--exam-only --exam-take --exam-submit` |

就绪体检会告诉你每场考试能不能考、卡在哪：

```
 【待完成 2 场】
  [中国商贸文化] 中国商贸文化   状态: 待做   截止: 105 天 2 小时
      就绪: × 暂时不能考（无需人脸）
            原因: 该考试教师已设置章节任务点未完成90%，不能参加考试
  [中华文化才艺] 中华文化才艺   状态: 待做   截止: 105 天 2 小时
      就绪: √ 可以考试（无需人脸；需要验证码）
```

进入考场后的安全设计（**与参考实现不同，刻意做的保守**）：

- **默认不自动交卷**：逐题作答并用 `tempSave` 逐题保存，答完提示你自己核对后交卷。
  只有第 3 档且**作答覆盖率达门槛**（默认 90%）才自动交卷；
- **不覆盖已答的题**：答题卡里显示"已答"的题直接跳过，不会把你自己答过的对题改错；
- **开考后出错会喊人**：一旦 `phone/start` 成功（计时开始），任何异常都会反复打印
  「请立刻打开手机/网页继续这场考试」以及剩余时间 —— 绝不让你在不知情的情况下被自动交卷；
- **开考失败不算消耗机会**：封面页/验证码/开考被拒，都在计时开始之前失败，返回 `ExamAborted`。

实现要点：走的是手机端协议（`exam-ans/exam/phone/*`），提交接口需要
`get_exam_signature()` 签名与虚拟 `imei`；**滑块验证码**用纯 PIL 模板匹配识别
（cv2/ddddocr 在源码里保留为可选加速路径，打包进 exe 的只有 PIL，代价约 +4.5MB）。

> ⚠️ **进考场之后的流程（取题/提交）无法离线验证** —— 那部分只能真跑。
> 所以第一次请用第 2 档（只答不交），核对无误后再用第 3 档。

> ⚠️ **实测结论（2026-09-11）：整卷模式需要 `openc`，这条路性价比不高。**
> 学习通考试的客户端有两种模式，本项目**保存答案这一步只在「单题模式」下验证过**：
>
> | 模式 | 保存接口 | 本项目 |
> |---|---|---|
> | 单题模式 | `reVersionSubmitTestNew` | 已实现 |
> | **整卷模式**（一页显示全部题） | `preview-save` + `paperId`/`examCreateUserId`/**`openc`** | 已实现，但 `openc` 拿不到（不在任何接口返回里，只在浏览器地址栏 URL 中），需要手动粘到 `config.ini` 的 `exam_openc` |
>
> `paperId` / `examCreateUserId` 能从整卷页里解析出来，唯独 `openc` 是网页客户端每次开考自己生成的。
> **所以：考试建议直接用浏览器里的油猴脚本**（如「超星网课助手(考试专版)」/ OCS）——
> 它跑在考试页里，天然拿到 `openc`、`paperId` 和页面状态，不需要逆协议，也不会因为改版失效。
> 本项目的价值在**刷课 + 章节测验 + 考试提醒**这三件它做得稳的事上；
> 考试作答这条路径保留着（默认关闭，需要 `--exam-take` 显式开启），但**不推荐作为主力方案**。



---

## 二、目录结构

```
.
├── README.md                    本文件
├── 说明文档.md                  完整排查/实测记录（含考试可行性报告、踩坑清单）
├── config.ini.example           配置模板（复制成 config.ini 再填）
├── start_chaoxing.bat           一键启动（Windows，GBK 编码）
├── 源码/
│   ├── chaoxing-fixed/          主程序源码
│   │   ├── main.py              入口：worker 线程池 + 看门狗
│   │   ├── api/answer.py        ★ 题库实现（题库链 / ANEVOL / 网课小工具 / AI 兜底）
│   │   ├── api/exam.py          ★ 考试看板（只读）
│   │   ├── api/base.py          超星接口封装（视频/文档/答题）
│   │   ├── resource/            字体映射表（题干乱码修复用）
│   │   ├── selftest_*.py        离线自测（见下）
│   │   └── smoketest_live.py    真实网络冒烟（会消耗额度）
│   └── anevol-bridge/           Plan B：本地桥接程序（用 TikuAdapter 方案时才需要）
└── 工具脚本/
    ├── anevol_quickcheck.py     单发探活：验证 ANEVOL 是否可用
    ├── probe_anevol.py          鉴权矩阵探针
    ├── analyze_concurrency.py   日志分析：统计视频并发分布
    ├── analyze_startup.py       日志分析：对比开局节奏
    ├── verify_exe_modules.py    离线校验 exe 里到底装了什么（解包内嵌 PYZ）
    └── publish_to_github.ps1    一键发布到 GitHub（自动脱敏 + 密钥扫描闸门）
```

---

## 三、快速开始

### 1. 准备

- Python 3.10+（开发时用的是 3.13）
- 一个超星（学习通）账号

```bash
pip install -r 源码/chaoxing-fixed/requirements.txt
```

### 2. 配置

```bash
copy config.ini.example config.ini
```

然后编辑 `config.ini`：

```ini
[common]
username = 你的手机号
password = 你的密码

[tiku]
; 查询顺序：从左到右问，第一个有答案的胜出（本地缓存永远最先查，不花钱）
; 【实测后重排 2026-09-12】AI 放到第一位：
;     ANEVOL 题库     20~42 秒/题   答出 5/8   ¥0.0015/题
;     直连 DeepSeek   0.5~2 秒/题   答出 8/8   ¥0.00014~0.00028/题
;   即慢 65 倍、还答不出来、还贵 5~10 倍。考试每题多等 30 秒 = 85 题多 40 分钟。
;   网课小工具命中率只有 3.5%（另有 473 次「未收录」），已从链路去掉；
;   想留作第三道防线，在末尾加回 ,TikuIcodef 即可。
provider = AI,TikuAnevol

; ANEVOL token（可选，不用就把 TikuAnevol 从上面删掉）
tokens = 你的ANEVOL_token

; AI 兜底（可选，不用就把 AI 从上面删掉）
endpoint = https://api.deepseek.com/v1
key = 你的DeepSeek_key
model = deepseek-chat
```

> 不填 token/key 也不会报错：未配置的题库会在启动时**明确提示并自动跳过**，其余照常工作。

### 3. 运行

**方式一：双击 `start_chaoxing.bat`（推荐）**

会先弹出交互菜单，**选模式 → 选课程 → 开始跑**：

```
 ================================================================
   chaoxing 修复版   一键启动
 ================================================================
    [1] 视频 + 章节答题模式
        刷课：视频 / 文档 / 阅读 / 章节测验（自动查题作答）

    [2] 考试模式
        只做考试看板 + 就绪体检（只读，不刷课、不替你进考场）

    [0] 退出
 ================================================================
请输入模式编号后回车: 2

   正在读取课程列表（需要登录，约几秒）...

    [1] 中国商贸文化
    [2] 中华文化才艺
    [0] 全部课程（共 2 门）

请输入课程编号（多个用英文逗号分隔，直接回车=全部课程）: 2

  已选择课程ID：266218735
```

> 课程菜单是启动脚本调用 exe 的 `--list-courses` 拉取的（纯文本 `序号|课程名|courseId` 走 stdout，
> 日志走 stderr，互不干扰），所以不用自己去查课程 ID。

**方式二：命令行**

```bash
cd 源码/chaoxing-fixed
python main.py -c ../../config.ini                  # 全部课程
python main.py -c ../../config.ini -l 266218735     # 只跑指定课程（可逗号分隔多个）
python main.py -c ../../config.ini --list-courses   # 只列出课程（给脚本用）
python main.py -c ../../config.ini --exam-only      # 只跑考试看板，不刷课
python main.py -c ../../config.ini --all-courses    # 全部课程，且不弹「手输课程ID」提示
```

> 注：原版走 `-c` 时会把命令行参数**全部忽略**（`-l` 指定的课程根本不生效）；
> 本仓库已修复为「命令行参数覆盖配置文件」。

### 4. 打包成 exe

```bash
cd 源码/chaoxing-fixed
pyinstaller --noconfirm --clean chaoxing-3.1.4-fixed.spec
```

产物在 `dist/`。spec 里已经包含 `resource/font_map_table.json`（不加会题干乱码）。

---

## 四、自测

全部为**离线自测**（用假响应，不联网、不消耗额度），共 **75 项断言**：

```bash
cd 源码/chaoxing-fixed
python selftest_anevol.py          # ANEVOL 答案映射链路（8 项）
python selftest_anevol_errors.py   # ANEVOL 错误路径 / 熔断（12 项）
python selftest_worker.py          # worker 线程自愈（5 项）
python selftest_icodef.py          # 网课小工具题库 + 并发串行化（18 项）
python selftest_chain.py           # 题库链回退 + 题号预处理 + 日志级别 + BOM（14 项）
python selftest_ai_fallback.py     # AI 兜底（12 项）
python selftest_cache.py           # 答案缓存并发写入竞态（4 项）
python selftest_exam.py            # 考试看板解析与提醒去重（7 项）
```

真实网络冒烟（**会消耗 ANEVOL 额度**）：

```bash
python smoketest_live.py -c ../../config.ini
```

离线校验某个 exe 里到底装了什么代码（不运行 exe、不联网）：

```bash
python ../工具脚本/verify_exe_modules.py dist/chaoxing-3.1.4-fixed.exe
```

---

## 五、⚠️ 安全提醒

**`config.ini` 里含你的账号密码和各类 token，绝对不要提交到公开仓库。**
本仓库的 `.gitignore` 已经忽略了 `config.ini`、`config-bridge.ini`、`cookies.txt`、
`cache.json`、`*.log`、`备份配置/`，只提供脱敏的 `config.ini.example`。

如果你 fork 了本仓库，提交前请自查：

```bash
git status
git diff --cached
```

---

## 六、已知限制

- **章节级串行**：同一个 worker 要等该章所有任务点（含答题）跑完才会接下一章，
  所以答题慢会**间接拖住**同 worker 的视频进度。这是原版设计，不是 bug。
- **覆盖率保护**：`submit=true` + `cover_rate=0.8` 时，覆盖率不足 80% 的章节**只保存不提交**，
  避免把随机答案交上去。接了 AI 兜底后覆盖率通常能到 100%，也就是**会真的自动交卷**，
  想稳一点可以把 `submit` 改成 `false`。
- 网课小工具题库对**专业课**覆盖较差（实测某专业课 0/123 命中），
  这类课程可以把 `provider` 里的题库都删掉，只留 `AI`，省掉无谓的等待。

---

## 七、致谢与许可

- 上游项目：[Samueli924/chaoxing](https://github.com/Samueli924/chaoxing)
- 题库接口：[网课小工具](https://cx.icodef.com/)、[ANEVOL](https://tiku.anevol.cn/)
- 本仓库沿用上游的 **GPL-3.0** 许可，详见 [源码/chaoxing-fixed/LICENSE](源码/chaoxing-fixed/LICENSE)

仅供学习与技术研究使用，请勿用于违反学校规定或服务条款的用途。
