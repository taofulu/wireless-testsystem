# Testbed 侧拉取式执行 Worker，Web 后端不直连仪表

执行链路采用拉模型：testbed 侧部署轻量执行 Worker，主动轮询 Web 后端拉取任务、本地执行 pytest、回传报告；通信为 REST。Web 后端只发任务，不与仪表/BBU 建立任何直连。

**Considered Options**: Web 后端直连 testbed 推任务——受 testbed 网络位置（NAT/防火墙/隔离网段）限制，且后端需持有设备连接凭据，攻击面大。消息队列——团队无 MQ 偏好，REST 轮询最简。

**Consequences**: Web 服务可部署在任意能访问数据库与 LASS API 的位置；Worker 的轮询间隔决定任务下发延迟；Worker 需具备断网重试与任务幂等领取能力。
