# tools/ —— 辅助脚本

这些脚本不是 `zhs` 的核心功能，而是围绕它做的本地工具。直接调用 `zhs` 包，**要在仓库根目录用 venv 里的 python 运行**：

```bash
cd ~/shua/zhs
.venv/bin/python tools/<脚本>.py ...
```

| 脚本 | 作用 | 典型用法 |
|------|------|----------|
| `qr_login.py` | 网页扫码登录：二维码放大到 380px、自动刷新、显示扫码状态 | `.venv/bin/python tools/qr_login.py` |
| `resume_homework.py` | 掌握度续跑：绕过 CAS 校验、按次数/起点控制作业 | `.venv/bin/python tools/resume_homework.py 课程ID 班级ID --max-tries 2` |
| `wrong_report.py` | 题目/错题导出为 Markdown | `.venv/bin/python tools/wrong_report.py --all` |

脚本读写的路径约定：

- 配置：仓库根 `config.toml`（脚本不直接读，交给 `zhs` 的 `ConfigManager`）
- 个人数据：`personal/`（cookie、缓存、日志、导出报告）
- `zhs` 使用的相对目录 `.zhs/` 是指向上述两者的软链目录，脚本不依赖它

详细说明见 [docs/使用指南.md](../docs/使用指南.md) 第 11 节。
