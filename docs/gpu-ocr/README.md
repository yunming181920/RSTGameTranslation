# RapidOCR + DirectML / Ollama 实时翻译分支

本分支在上游 `d39f45dccbb505c87a0d18b69bdf8091f6d865af` 上保存当前经过验证的实现：GPU OCR、`||` 批量翻译、过期请求取消、原文附近小号旁注及测量缓存。

## 环境与构建

- Windows x64、.NET 9 SDK/桌面运行时、Python 3.12。
- 支持 DirectML 的显卡与驱动。本地验证设备为 AMD RX 9070 XT。
- Ollama 与一个已安装的翻译模型；本次验证使用 `index-translate-2b`（Q8_0）。

标准工程仍为根目录 `RST.csproj`：

```powershell
dotnet build RST.csproj -c Release
```

若已有可用的 RST 发行包，也可用兼容工程复用它的依赖DLL：

```powershell
dotnet build RST.Local.csproj -c Release -p:RstReleaseDir="C:\RST-release"
```

该工程输出到 `artifacts/local/`，使用 `asInvoker` manifest。在发行包的一份副本中替换构建出的 `rst.exe`、`rst.dll`、`rst.pdb`、`rst.deps.json`、`rst.runtimeconfig.json`，保留发行包的语言、字体、OCR及音频等资源。此方式对应已验证的本地构建流程；并不把整个旧发行包提交到Git。

本地工程使用.NET 9框架依赖启动方式。如果发行包副本中有旧自包含运行时的`hostfxr.dll`，先将它改名备份，使用`-RuntimeDir`明确指定.NET 9目录，避免旧host接管新的exe。

## 安装 GPU OCR

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File .\gpu_ocr\setup.ps1
```

创建独立的 `gpu_ocr/.venv` 并安装固定的三个直接依赖，随后检查检测、方向分类、识别模型是否都注册了DirectML。`requirements.lock.txt` 是实际隔离OCR目录的依赖版本快照；不包含用户全局Python环境中的其他软件包。首次验证如果缺少模型，需要联网获取RapidOCR模型。

Ollama代理只使用Python标准库，可以复用同一个虚拟环境。没有必要单独安装Conda。

## 启动

将真实Ollama服务设在11438，翻译代理使用11434。代理默认转发 `http://127.0.0.1:11438`；Ollama模型应事先安装。

如果exe位于仓库的 `app/`：

```powershell
.\start_rst_gpu.ps1
```

或者双击根目录 `启动RST_GPU测试版.cmd`。若exe放在发行包副本中：

```powershell
.\start_rst_gpu.ps1 -AppDir "C:\RST-test" -Model "index-translate-2b"
```

脚本完成：检查/启动Ollama → 模型预加载并驻留30分钟 → 检查/更新本仓库代理 → 启动exe。代理端口若被其他程序占用会停止，不会结束不相关的进程。`-ServicesOnly`只准备服务。

可选参数：`-RuntimeDir`、`-OcrPython`、`-ProxyPython`；对应环境变量为 `RST_DOTNET_ROOT`、`RST_OCR_PYTHON`、`RST_PROXY_PYTHON`。例如安装了多个.NET版本时可明确指定.NET 9目录。脚本为子进程传入OCR Python与backend路径，无需建立指向本机其他目录的junction。

代理日志默认写入当前用户的 `%LOCALAPPDATA%/RSTGameTranslation/proxy.log`，可以用 `RST_PROXY_LOG` 指定。`RST_OLLAMA_UPSTREAM`可指定其他本机HTTP上游地址；一键启动脚本的默认端口仍为11438/11434。

RST中选择 **RapidOCR (DirectML)**、Ollama接口 `http://127.0.0.1:11434/api/generate`、源语言日语、目标简体中文。选择需要识别的区域后点击“开始”。

## 当前行为

- 按源语言判断是否翻译；“体力回復”等纯汉字日文不会被误判为已是中文。
- 最多12个新片段、900字符拼接为一次模型请求，以 `||` 分隔；保留RST内部 `##|||##` 格式及块ID。字面竖线单独处理。
- 使用已验证的日译中提示；保留数字/变量/格式标记。明显日文残留或丢失保护标记时，只对失败段纠正一次，失败结果不写缓存。
- 开启“隐藏过期译文”时，画面变化会取消旧请求/预取，通过代理关闭上游套接字，让当前画面优先。未完成的旧页不保证生成完整历史译文。
- 活跃125ms采样；严格相同画面复用候选OCR完成稳定确认。保留200ms稳定阈值、逐字文本保护和周期重查。
- 启动时后台加载DirectML；重用BMP缓冲。原文附近的旁注使用字号/测量缓存，支持多行避让。
- “旁注布局”可在叠加设置中切换；保留原覆盖布局。

个人 `config.txt`、API密钥、下载模型、Python运行时、编译输出和本机备份均不属于本次新增源码提交。

## 测试

代理回归不需要模型或GPU：

```powershell
python tests/proxy/test_proxy.py
python tests/proxy/test_batching.py
python tests/proxy/test_pipe_separator.py
python tests/proxy/test_repeat_guard.py
```

Windows下测试实际帧比较、缓存与WPF测量，可以引用已构建的同版本DLL：

```powershell
dotnet run --project tests/core/CoreTests.csproj -c Release -p:RstAssembly="C:\RST-test\rst.dll"
```

不指定`RstAssembly`则引用标准`RST.csproj`。真实模型集成测试需要11438上的上述模型，会使用独立代理实例：

```powershell
python tests/integration/live_cancel.py
python tests/integration/verify_translations.py
```

测试结果写入被忽略的`tests/artifacts/`。延迟数据的范围和限制见 [BENCHMARKS.md](BENCHMARKS.md)。
