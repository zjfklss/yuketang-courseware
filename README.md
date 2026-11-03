# 雨课堂课件

把本人已加入的雨课堂课件（课堂幻灯片）收成 PDF，图形界面点着用，也可以命令行整批下载。课件仅供本人学习，不要再传播。

平台只给分片图片，不给 pptx，所以输出是 **PDF 图片版**：每页一张原图，JPEG 字节直接嵌进 PDF（`/DCTDecode`），不解码、不重编码，画质和文件大小都跟平台上的原图一致。少数课件在平台上挂了直链 PDF，本工具也按 PPT 图片流处理，结果一样是 PDF。

程序不读取日常 Edge / Chrome 的 Cookie 库，也不把学校 id、学号、姓名写死。登录发生在单独的浏览器配置目录里：`~/.local/share/yuketang-courseware/browser`。学校 id 与课堂 id 只从你填的网址里读。

## 安装包

```bash
cd tools/courseware
./build-deb.sh                             # 只用 dpkg-deb，不需要 debhelper
sudo apt install ./dist/yuketang-courseware_0.5.2_all.deb   # apt 会把依赖一起装好
```

用 `dpkg -i` 也行，缺依赖时再 `sudo apt-get install -f`。

依赖：`python3`（≥3.8）、`python3-gi`、`gir1.2-gtk-4.0`（≥4.6）、`gir1.2-adw-1`（≥1.0，libadwaita 界面）、`python3-pil`（只在遇到非 JPEG 页时用到）。另外系统里要有 `microsoft-edge` 或 `google-chrome`，启动浏览器时才找；两个都没有时界面和命令行都会提示。

装好后：应用菜单里点「雨课堂课件」，或在终端运行 `yuketang-courseware`。命令行手册：`man yuketang-courseware`。

### 换一台电脑跑

装完先在目标机器上跑一次自检，缺什么它会直接说：

```bash
yuketang-courseware --doctor
```

输出里有 Python 版本、Gtk 4 / libadwaita 版本、浏览器路径、配置目录是否可写，缺的会给出 apt 命令。图形界面要 Gtk4 + libadwaita，命令行只要 Python。

各发行版的实际情况：

| 系统 | Python | GTK / libadwaita | 图形界面 | 命令行 |
|---|---|---|---|---|
| Ubuntu 24.04 | 3.12 | 4.14 / 1.5 | ✓ | ✓ |
| Ubuntu 22.04 | 3.10 | 4.6 / 1.0 | ✓（低版本 API 有兜底） | ✓ |
| Debian 12 | 3.11 | 4.8 / 1.2 | ✓ | ✓ |
| Ubuntu 20.04 / Debian 11 | 3.8 / 3.9 | 只有 GTK3 | ×（没有 Gtk4） | ✓ |

代码里对高版本 API 都做了能力检测：`Adw.ToolbarView`（1.4+）、`Adw.Banner`（1.3+）、`Adw.AlertDialog`/`AboutDialog`（1.5+）、`Gtk.FileDialog`（4.10+）缺了就走替代实现，所以 22.04 上也能开窗口。

**为什么不重写成 C++**：换语言解决不了这里的故障。让人跑不起来的两个原因是 Python 注解写法（已经在 `errors.py` 补了 `from __future__ import annotations`）和 **Gtk4 / libadwaita 的版本**——C++ 用的还是同一套 GTK 库，同一个版本上照样崩。反过来还要自己重做 WebSocket、DevTools、JSON、PDF 这几层，代码量翻几倍，安装包从 `Architecture: all` 变成按架构编译。真要「一个包拷过去就能跑」，正确做法是把 Gtk4 + libadwaita 一起打进 Flatpak / AppImage，那跟用什么语言写无关。

## 下载之后的目录

保存目录（你选的那个）下面：**每门课一个文件夹 → 再一个 `课件` 文件夹 → 每个讲次一个 PDF**，和本知识库现有 `机械设计/课件/`、`脑科学/课件/` 的排法一致。

```text
<你选的保存目录>/
├── 自动控制原理/
│   └── 课件/
│       ├── 第01讲 第1章绪论.pdf
│       ├── 第12讲-第7章（1）.pdf
│       └── 第12讲-第7章（1）-1.pdf      ← 一讲有多个幻灯片时加 -1、-2
├── 计算机网络/
│   └── 课件/
│       ├── 第10讲 计算机网络(5)-传输层 -UDP&TCP协议.pdf
│       └── 第11讲 计算机网络(6)-应用层 - DNS&DHCP&FTP&TLNET-V2.pdf
└── 计算方法/
    └── 课件/
        ├── 第1章-绪论-2学时.pdf
        └── 第二章 插值法-1-1.pdf
```

几个约定：

- 课程名用平台返回的课程全名（例如 `自动控制原理`，不是你自己简写的 `自动控制`）；同一学期里课程名重复的班级，文件夹名后面带班级名。
- 没有幻灯片、下载失败的讲次**不会**生成空文件或占位文件，只在结果里说明。
- 已有的文件不会被覆盖，也不会被删除；同名但内容不同的文件只报告冲突。
- 唯一的例外：页数少于 3 的 PDF 视为没下完，会重下一次（同名不同内容时不静默跳过）。
- 下载结束后，界面「下载结果」里会列出保存位置，并可以展开每个课程文件夹看实际文件；命令行直接打印上面这棵树。

## 目录里已有的文件

程序**递归**扫描你选的保存目录（包括多级子目录），按「讲次名」和已有 PDF 对上号，对上的直接跳过，不会重复下载——哪怕这些文件是以前手工下的、或者被你自己挪到别的子文件夹里了。

- 文件名比较时忽略空格、标点、全半角、大小写，**中文数字按阿拉伯数字算**：`第04讲-第二章（3）(3).pdf`、`第04讲 第二章 3 3`、`第四讲 第2章(3).pdf`、`第４讲 第二章（三）.pdf` 都视为同一讲。
- 我们自己的多套命名（`标题-1.pdf`、`标题-2.pdf`）也认，但每个文件只会被一讲认领，不会两讲抢同一个文件。
- 名称对不上时**按内容核对**：先算 PDF 页数（对象头里读 `/Count`，读不到就用 `pdfinfo` 兜底），和平台这一讲的幻灯片页数比；页数一致就认定是同一讲，跳过不重复下。
- 自己下过的文件记内容指纹（SHA-256）到 `~/.cache/yuketang-courseware/downloaded.json`，以后即使被改名、被挪到别的子文件夹，也照样认出来。
- 识别到的数量会显示在界面「目录里已有」那一行，命令行在下载前也会报「已有 N 个 PDF，其中 M 个对得上」。

要顺手**整理**：界面点「保存 → 整理」，或命令行加 `--tidy`，程序会把对得上的 PDF 挪进 `课程名/讲次名.pdf` 的标准位置。先看计划不执行就加 `--dry-run`。

整理按五条走，和 aria2、Syncthing 这些老工具一个路子：

- **跟着你已经有的目录走**。你把这门课放在 `自控/` 里，就不在同一个保存目录再建一个 `自动控制原理/`；你已经给它起了个存放子目录（`自控/自动控制课件/`），就沿用它，不在旁边再建一个空的 `课件/` 把文件搬走。
- **一讲几套幻灯片都归位**：`第4讲.pdf`、`第4讲-1.pdf` 一起搬，不会只搬一个。
- **绝不覆盖**。目标位置已有同名文件、内容又不一样时，来的这份另存成 `第4讲 绪论（另一版本 2026-11-02）.pdf`，两份都留在原地让你自己看页数决定；同一个目标最多留 10 份，再多就只报告不动。内容完全一样则只报「重复」，不删。两份都已经在课件目录里、只是名字差个后缀时原样留着，不再另存副本。
- **残片不另存**。页数少于 3 的（没下完、拷到一半）报成冲突让你自己删，不当成「另一个版本」保存进去。
- **认不出来的文件不动，但会列出来**，免得你以为整理完就都归位了。只列课件范围内的；`笔记/`、`习题/`、`提交/`、`figs/` 这些子目录里的 PDF 不是课件，只报一个总数（「另有 N 个不在课件目录里」）。`_archive/`、`_归档/`、`.trash/` 整个跳过——归档里的旧课件不该被当成「另一讲版本」搬出来复活。

要「只报告不另存」就加 `--on-conflict skip`。转移到另一个分区（保存目录在 U 盘）时会自动改成先复制再删源，不会半路失败。

## 图形界面

装好以后从应用菜单打开「雨课堂课件」，不用看文档也能走完。

界面截图没有放进仓库——截图里会带上真实的课程名和班级名。想看自己的界面长什么样，直接开窗口，或者用 `python3 tests/screenshot.py --state fresh|populated --out xxx.png` 生成一份放本地。

四步，全在窗口里：

1. **登录雨课堂**（第 1 步）。先在自己的浏览器里打开雨课堂，随便点开一门课，把地址栏整条网址复制过来，粘进「雨课堂网址」，点「登录雨课堂」。不同学校、不同人的入口不一样（例如 `https://xxx.yuketang.cn/v2/web/index`），程序不预置任何学校地址；网址里的 `classroom_id`、`university_id` 只从你粘的这条里读。程序用独立配置目录打开 Edge 或 Chrome：没登录就停在登录页，在那个窗口里扫码或用账号登录，它跟平时用的浏览器是两套配置，互不影响。登录成功后，课程会自动出现在下面，不用再点别的。窗口里也有「怎么拿到网址？」的三步提示。
2. **选课**（第 2 步）。学期下拉框用接口返回的 `term` 原样显示，默认最新；打开「全部学期」就把往年的课一起列出来（标题后面标学期）。点开一门课能看到每一讲：`✓` 可下载，`（课件区）` 表示来自课件区，灰掉的是不可下载的（例如网页预习）。讲次默认全勾；「全选所有课程」一键切换。
3. **课程与讲次**（第 3 步）。勾选要下载的讲次；目录里已有的会自动跳过，截图下方能看到每门课每一讲的勾选状态。
4. **选目录并下载**（第 4 步）。保存目录不预设：点「选择」挑一个文件夹，或者直接点「开始下载」，程序会先弹选择框，选完直接开始下。「打开」在文件管理器里打开它。每门课一个子文件夹，目录名是课程名；同一学期课程名相同的班级，目录名带上班级名。

下载过程中可以点「停止」，手上那一讲做完就停。完成后底部弹结果，并出现「下载结果」一栏：全部成功只写一行；有没下下来的（已有跳过 / 无幻灯片 / 失败）逐条列出，并写清哪一讲缺了第几页。

同名文件若已经是完整 PDF（文件头 `%PDF-`、末尾 `%%EOF`、至少一页），则跳过不覆盖；文件坏了或只有半截，会重下。一讲只有一套幻灯片时文件名是 `标题.pdf`，有多套时是 `标题-1.pdf`、`标题-2.pdf`。上次粘的网址和上次选的目录会记住（`~/.local/share/yuketang-courseware/settings.json`）。

幻灯片页面在内存里拼装，不落临时文件。某一页下载失败只少这一页，其余页照常合成，缺的页号写在状态里；每页最多试 3 次，间隔递增，6 页并发。

## 命令行

```bash
yuketang-courseware --url '<粘贴的网址>' --list
```

打开登录窗口，等登录完成后列出能拿到的讲次，不下载。

```bash
yuketang-courseware --url '<网址>' --all --out ~/课件            # 整批下载（最新学期）
yuketang-courseware --url '<网址>' --all --course 自动控制       # 只下课程名含「自动控制」
yuketang-courseware --url '<网址>' --all --all-terms             # 连同往年的课
yuketang-courseware --url '<网址>' --all --source leaf           # 只要课件区的
yuketang-courseware --url '<网址>' --all --no-open --wait 30     # 用已经开着的登录窗口
```

`--url` 必填（从地址栏复制）。常用开关：`--out` 保存目录（默认当前目录）、`--tidy` 整理已有文件、`--dry-run` 只看整理计划、`--course` 课程关键词（可重复）、`--term` 指定学期、`--all-terms` 全部学期、`--source {both,activity,leaf}`、`--jobs` 同时几讲（默认 1）、`--delay` 课间隔秒数（默认 0.5）、`--wait` 等登录秒数。退出状态：`0` 成功，`1` 出错或下载有失败项，`2` 参数不对。

命令行不依赖 GTK；没有图形界面时照样能跑（浏览器窗口还是要开，登录在窗口里做）。

## 速度

整理讲次原来是一门课接一门课、每次换 `courseware_id` 都要等一次接口，11 门课 120 次请求要 18 秒左右，界面看起来像卡住。现在：

- 换 `courseware_id` 的请求按 6 路并发（同一门课内），课程按 3 路并发读；
- 课程一读完就立刻出现在列表里（边读边渲染），进度条按「读完几门 / 共几门」走；
- `leaf → courseware_id` 的对应关系记在本机缓存 `~/.cache/yuketang-courseware/leaf-map.json`（7 天），同一个课再跑不用再问一遍；
- 课堂活动里已经有的讲次，不再去课件区换一次 `courseware_id`（一个 leaf 一个请求，换回来也是被合并丢掉）；
- 每个请求不再重复「扫 `/proc` 找浏览器调试端口 + 列一遍标签页」——一次登录只做一次，之后复用；
- 登录轮询 1.5 秒一次，扫完码通常一两秒内就出列表。

本地假服务端实测（11 门课 / 264 讲，模拟往返 100 毫秒）：

| | 接口请求 | 首次 | 第二次 |
|---|---|---|---|
| 改之前 | 298 次 | 3.1 秒 | 2.5 秒 |
| 现在 | **34 次** | **1.4 秒** | 1.4 秒 |

真机上差距更大：改之前每个请求都要扫一遍 `/proc`（几百个进程、浏览器上千个 fd），每次几十毫秒，几百次请求就是几十秒。

想知道自己机器上慢在哪，加一个环境变量启动，每个请求的耗时打到 stderr，结束时给汇总（共几次、累计几秒、最慢的是哪个接口）：

```bash
YUKETANG_TIMING=1 yuketang-courseware --url '<网址>' --all --out ~/课件
```

另外三条防卡死的处理：

- 浏览器会把切到后台的标签页冻结，冻结后调试求值会一直等到超时（看起来就是「卡住、没反应」）。现在启动时关掉后台节流/冻结，遇到没响应先唤醒页面（前置 + 生命周期设为 active）再重试一次，等待也有上限，不会无限挂。
- 窗口被关掉但浏览器进程还在时，程序会把雨课堂页面重新开回来（登录态还在配置目录里），空会话自愈后第一次请求实测 0.8 秒。
- 整理讲次没完成时点「开始下载」不再毫无反应，会提示「还在整理讲次，等进度条走完再点下载」。

## 接口

课程：`GET /v2/api/web/courses/list?identity=2`。

课堂活动：`GET /v2/api/web/logs/learn/{classroom_id}?actype=-1&page=&offset=100&sort=-1`，按 `has_more` 翻页。可下载的是 type 14 和 type 3。

课件区：`GET /v2/api/web/classrooms/{classroom_id}?role=5` 拿 `course_sign` 和 `uv_id` → `GET /mooc-api/v1/lms/learn/course/chapter?cid=&sign=&term=latest&uv_id=&classroom_id=` 拿 leaf，只留 `leaf_type=8` → `GET /edu_admin/leaf_level_info/?leaf_level_id=` 拿 `courseware_id`。

每一讲：`GET /api/v3/classroom-report/student/review?lesson_id=&front_time=` 收集不重复的 `presentationId` → `GET /api/v3/classroom-report/student/ppt?lesson_id=&presentationId=&front_time=` 取 `slideList` → 下载有 `cover` 的页 → 合成 PDF。封面地址自带 token，只在本进程里请求，不写进状态、不写进文件名。

课堂活动与课件区合并时按 `courseware_id` 和标题去重，课堂活动优先——同一次课在两边都出现时只下一次。

接口契约对照 [DDDDDK1/rainclassroom-ppt-downloader](https://github.com/DDDDDK1/rainclassroom-ppt-downloader) 的 `docs/apis.md`（抓包确认：无动态加密签名，`sign` 就是课程详情返回的 `course_sign`）。本工具只采用它的接口顺序（review → ppt → cover → PDF），不采用它的油猴脚本，不采用文档里的学校 id，不解密日常浏览器的 Cookie。

## 自测

```bash
cd tools/courseware
python3 -m unittest discover -s tests -v
```

单测不联网：用假 fetch 和本地生成的 JPEG 校验课程解析、两条来源合并、PDF 合成与页数、缺页隔离、跳过已完整文件。`tests/test_robustness.py` 专门喂畸形输入（null 返回、半张图、`../` 文件名、坏缓存文件、几千文件的目录）；`tests/test_fetch_perf.py` 用本地假 DevTools 服务端（`tests/fake_devtools.py`）卡住两件事：重复请求不许再扫 `/proc`、浏览器已开着时再点「登录」不许死锁。

量性能用这两个脚本（同样不联网、不起浏览器）：

```bash
python3 tests/bench_fetch.py 60              # 每次请求的自耗（不含平台网络）
python3 tests/bench_load.py 11 24 100        # 11 门 24 讲、模拟 100ms 往返，数请求
```

`tests/screenshot.py` 把窗口渲染成 PNG（需要图形界面，不联网），用来核对界面：

```bash
python3 tests/screenshot.py --state fresh     --out ~/界面-空.png
python3 tests/screenshot.py --state populated --out ~/界面-有课.png
```

要真正验证下载，得在浏览器里登录一次。

## 目录

```text
tools/courseware/
  yuketang_courseware/   # 源代码
    __main__.py          # 无参数开窗口，带参数走命令行
    app.py               # libadwaita 窗口
    cli.py               # 命令行批量下载
    catalog.py           # 课程、课堂活动、课件区
    slides.py            # 下载页面、合成 PDF
    library.py           # 递归扫描目录、认已有文件、整理（只搬不删）
    pdf.py               # JPEG 直嵌的 PDF 写入器
    browser.py           # 独立配置目录的浏览器 + DevTools 取数
    cdp.py               # 极简 WebSocket / DevTools 客户端
    names.py             # 网址解析、文件名净化、抹掉凭据
    settings.py          # 上次的网址和保存目录
    errors.py            # 界面可直接显示的错误
  packaging/             # 启动脚本、.desktop、man 页；make-icon.py 生成图标（SVG + 各尺寸预览）
  tests/                 # 离线单测 + 界面截图
  docs/                  # 图标预览图（界面截图不放仓库，含真实课程名）
  build-deb.sh           # 打 deb
  dist/                  # 安装包
```
