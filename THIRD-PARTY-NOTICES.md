# 第三方依赖与标识

PAL 自身以 MIT 许可分发，原始许可文本见 [LICENSE](LICENSE)。运行依赖独立安装，不把它们的
源码并入 PAL。各依赖仍以其自身许可证和发行文件中的版权声明为准。

下表对应本次锁文件的运行依赖，不替代上游完整许可；升级依赖时需重新核对。
普通 wheel/工具安装按包声明的版本范围解析依赖，不强制使用源码的 `uv.lock`；实际安装版本
可能不同。锁文件用于源码开发与本次验收的依赖复现。

| 包 | 锁定版本 | 许可证 | 来源 |
|---|---|---|---|
| jsonschema | 4.26.0 | MIT | [python-jsonschema/jsonschema](https://github.com/python-jsonschema/jsonschema) |
| markdown-it-py | 4.2.0 | MIT | [executablebooks/markdown-it-py](https://github.com/executablebooks/markdown-it-py) |
| attrs | 26.1.0 | MIT | [python-attrs/attrs](https://github.com/python-attrs/attrs) |
| jsonschema-specifications | 2025.9.1 | MIT | [python-jsonschema/jsonschema-specifications](https://github.com/python-jsonschema/jsonschema-specifications) |
| mdurl | 0.1.2 | MIT | [executablebooks/mdurl](https://github.com/executablebooks/mdurl) |
| referencing | 0.37.0 | MIT | [python-jsonschema/referencing](https://github.com/python-jsonschema/referencing) |
| rpds-py | 2026.6.3 | MIT | [crate-py/rpds](https://github.com/crate-py/rpds) |
| typing-extensions | 4.16.0 | PSF-2.0 | [python/typing_extensions](https://github.com/python/typing_extensions) |

markdown-it-py 和 mdurl 保留了其 JavaScript 上游的 MIT 版权说明；上游许可随各自包分发。
pytest、Ruff、uv 及审查工具用于开发验证，不属于 PAL 运行依赖。使用分发工具时仍应遵守其许可。

PAL 字标与图标使用本项目原创 SVG 路径，不依赖外部字体。仓库中的文字和标识遵循本项目
许可；许可证不代表授予第三方商标或官方背书。Claude Code、Codex、Anthropic、OpenAI 等名称
用于说明兼容对象，其商标属于各自权利人。PAL 与这些厂商没有隶属关系。
