# SWATPlus Workbench

Windows 桌面 SWAT+ 科研工作台，支持资料准备、自动建模、已有模型接回、模型运行、参数分析和管理情景。界面使用简体中文，计算通过已有脚本、QSWAT+、Editor 和 SWAT+ 执行。

首个预发布版本为 **0.0.1-beta.1**，作者为 Xun Zhang，项目按 GPL-3.0-or-later 发布。

## 安装与启动

1. 在 Releases 下载 `SWATPlus-Workbench-0.0.1-beta.1-setup.exe`，运行并选择可写的安装目录。默认安装在当前用户的本地应用目录中。
2. 安装后打开桌面 `SWATPlus Workbench` 快捷方式。
3. 首次启动查找 QGIS、QSWAT+、Editor、SWATPlus 支持目录和 MPI。识别成功后进入工作台。自定义目录未识别时，在窗口中选择目录并保存。
4. 在“资料准备”载入教学示例，进入“新建任务”，先检查配置，再运行。

也可下载 `win-x64.zip`，解压到可写目录并打开 `SWATPlus Workbench.exe`。保留 EXE、Workbench 和 runtime 的相对位置。主界面“环境设置”可重新查找已移动的软件。

安装包随附 Python 3.9.1、Tkinter 和科学计算依赖。Python 运行环境是本版本的兼容性组合。第三方组件许可证随包保留。程序和安装包使用 Windows 自带 .NET Framework 4.8，当前支持 Windows 10/11 x64。

## 外部环境

完整自动建模需要预先安装以下兼容组合。

| 组件 | 支持范围 |
| --- | --- |
| QGIS | 3.44 LTR，包含 `bin/python-qgis-ltr.bat` |
| QSWAT+ | 4.0.4，匹配 QGIS 的 Python 扩展 |
| SWAT+ Editor | 4.0.2，含 API 与官方 SWAT+ 62.0.1 引擎 |
| SWATPlus 支持目录 | `Databases` 与 `TauDEM539Bin` 完整 |
| Microsoft MPI | 可定位 `mpiexec.exe` |
| Toolbox | 4.0.1，用于 Toolbox 率定及结果回收，可按需配置 |
| 原版 SUFI-2 | 经典 SWAT-CUP 的统计程序目录，使用相应算法时配置 |

安装盘符和用户名可以不同。路径选择窗口支持 Editor 独立安装目录。支持目录内部仍需保持官方数据库与 TauDEM 的文件结构。自动发现读取相关软件的注册表安装记录、常用安装位置和 QGIS 用户插件目录，操作只更新工作台自己的配置。

QGIS/QSWAT+ 下载入口 https://qgis.org/ 与 https://swat.tamu.edu/software/plus/

## 工作目录与合作资料

`Workbench/config` 保存本机环境和用户方案，`Workbench/runs` 保存任务与结果。安装器重新安装时保留已有配置、资料和模型文件。请为长期合作案例选择清晰的独立资料目录，并在保存方案时确认文件位置。

随附 Robit 为官方教学资料生成的 SWAT+ 原生输入，示例运行 1990—1991 年，预热 1 年。其用途是检查软件运行流程。真实流域的水量平衡、观测率定和独立验证需要另行完成。岷江、南小河沟等合作研究资料由研究者另行共享。

## 源码运行与构建

源码用户准备 Python 3.9 与依赖后，运行 `python -B -X utf8 Workbench/scripts/portable_startup.py`。安装依赖使用 `python -m pip install -r distribution/requirements-runtime.txt`。

Windows PowerShell 7 构建示例可在当前仓库目录执行。

```powershell
& '<Python目录>\python.exe' distribution/build_release.py --python-root '<Python目录>' --output dist
```

构建脚本选择性复制该 Python 的标准库、Tkinter 与已安装依赖，再调用 Windows .NET Framework C# 编译器生成便携 ZIP 和安装 EXE。所有构建输入应来自可信的兼容环境。

诊断可运行 `runtime/python.exe -E -s -B -X utf8 Workbench/scripts/portable_startup.py --diagnose`。

## 当前验证与边界

本版本的实际检查及剩余限制见 `RELEASE_NOTES.md`。研究引擎可从界面显式选择兼容版本的 EXE。Fortran 编译环境与引擎源码由独立项目提供。

源码中的过程与参数接口沿用既有实现。新增水文方程、LAI 同化和地下水漏斗治理机制需要依据科学问题另行开发、率定和验证。

## 许可证与第三方来源

工作台源码按根目录 GPL-3.0-or-later 发布，第三方组件遵循各自许可证。Python 和依赖的许可证保存在 runtime 中。外部 QGIS、QSWAT+、Editor、TauDEM、MPI、Toolbox 和 SUFI-2 由使用者自行安装。来源说明见 `THIRD_PARTY_NOTICES.md`。
