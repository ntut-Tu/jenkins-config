# Jenkins Config

用 Docker Compose 啟動 Jenkins controller 與 agent，並建立 `jenkins-pipelines` 定義的工作。
部署時從 `settings.local.yaml` 產生 Compose 與 Jenkins 設定檔。

## 部署

需要 Docker Engine（或 Docker Desktop）、Compose v2、Python 3.12 以上與 [uv](https://docs.astral.sh/uv/getting-started/installation/)。
映像由 `jenkins-images` 專案提供。

```bash
cp settings.example.yaml settings.local.yaml
```

依 [設定範本](settings.example.yaml) 的註解編輯 `settings.local.yaml`，再執行：

```bash
./deploy.sh
```

依範本部署時，開啟 `http://localhost:18080/`。管理員帳號由 `jenkins.admin.user` 設定；
若 `jenkins.admin.password` 為 `null`，首次部署產生的密碼存於 `.secrets/admin_password`。

## 設定位置

| 設定 | 用途 |
| --- | --- |
| `images.controller`、`images.agent` | 指定要部署的映像版本或 digest |
| `jenkins.url`、`jenkins.http_port` | 設定 Jenkins 網址與主機連接埠 |
| `pipeline.repository.url`、`pipeline.repository.branch` | 指定工作定義所在的 Git 專案與分支 |
| `credentials.pipeline_git` | 私有 Pipeline 專案的 Git 帳號與 token |
| `credentials.application_git` | 私有被測專案的 Git 帳號與 token |

被測專案的 URL 與分支寫在 `jenkins-pipelines` 的 Jenkinsfile。
`settings.local.yaml` 已被 Git 忽略；不要提交含密碼或 token 的設定檔。

## 常用指令

以下指令都在本專案目錄執行：

| 指令 | 用途 |
| --- | --- |
| `uv run --locked python -m jenkins_config status` | 查看容器狀態 |
| `uv run --locked python -m jenkins_config seed` | 從 Pipeline 專案更新工作定義 |
| `uv run --locked python -m jenkins_config reload` | 重新載入 Jenkins 設定與工作定義 |
| `uv run --locked python -m jenkins_config down` | 停止容器，保留 Jenkins 資料 |

## 測試

```bash
uv run --locked python -m unittest discover -s tests -v
```
