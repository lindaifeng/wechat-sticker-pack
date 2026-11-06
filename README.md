# 一句成套：微信表情制作 Skill

给 AI 助手一句需求和一张有权使用的角色参考图，制作一套微信表情专辑素材。默认先生成 1 张动态预览，经用户确认后补齐至 8 张，并准备封面、聊天面板图标和详情页横幅。

## 使用

将本仓库安装为 AI 助手的 Skill，例如对支持 Skill 安装的 Codex 说：

> 请把这个 GitHub 仓库安装为本地 Skill： https://github.com/lindaifeng/wechat-sticker-pack

然后提供角色参考图并说明风格、聊天场景及是否配字，例如：

> 使用 wechat-sticker-pack，根据这张我有权使用的小狗照片制作 8 张有趣的微信动态表情。先生成一张给我确认，文字或符号跟随动作出现。

GIF 或图片生成能力取决于实际使用的 AI 环境，此 Skill 不包含生图模型、API 密钥，也不代替微信投稿或保证审核通过。涉及图片上传和外部服务时应按环境权限及用户授权执行。

## 导出与检查

需要 Python 3.9+ 和 Pillow。可先安装依赖 `python3 -m pip install Pillow`，并按 [SKILL.md](SKILL.md) 准备 `pack.json`、表情和配套素材；然后运行：

```bash
python3 scripts/sticker.py build <工作目录>
python3 scripts/sticker.py check <工作目录>
```

`submit/` 包含已提供的投稿素材，`预览.html` 可播放动图，`交付给客户.zip` 包含 `submit/` 和上架文案。缺失素材不会自动补齐；机器检查不替代文字、动作、版权及微信审核的人工核对。规格快照见 [微信规则](references/wechat-rules.md)，提交时以平台当时的要求为准。

## 许可

本项目采用 [MIT 许可证](LICENSE)。
