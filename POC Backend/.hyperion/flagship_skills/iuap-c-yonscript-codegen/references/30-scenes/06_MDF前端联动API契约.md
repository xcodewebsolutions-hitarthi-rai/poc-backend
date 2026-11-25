# MDF 前端联动 API 契约

> **加载时机**：公有云 MDF 前端需要调用 `cb.rest.invokeFunction` 查询业务对象、ID 转编码/名称、补充报表参数、页面跳转参数或回填字段。  
> **职责**：只回答“何时必须生成后端 API、API 返回结构如何驱动 MDF 解析”。不讲通用 YonQL 模板、不讲 MDF 按钮写法。

---

## 1. 场景识别

只要 MDF 前端拿到的是 ID，但后续需要 code/name/其他业务字段，就必须识别为前后端联动场景。

典型场景：

- 查询条件中只有标段信息 ID，但自由报表需要标包编码。
- 参照字段只有 id，但页面要展示或传递参照对象 code/name。
- 按钮点击后需要读取业务对象详情再跳转报表或页面。
- 列表/卡片前端没有现成字段，需要跨对象查询补值。

---

## 2. 生成顺序

```text
识别 MDF 需要查询
  → 判断前端是否已有目标值
  → 没有则生成/确认 YonScript API
  → 明确 API 入参
  → 明确 API return 结构
  → 说明平台包装后的前端回调结构
  → 协同 MDF 按该结构解析
```

禁止只生成前端占位：

```javascript
// ❌ 不允许只留下 yourApiFunctionCode
cb.rest.invokeFunction('yourApiFunctionCode', params, callback);
```

---

## 3. API 返回契约

API 脚本必须返回稳定、简单、前端易解析的结构。

推荐：

```javascript
return {
  success: true,
  sectionId: sectionId,
  sectionCode: String(sectionCode)
};
```

失败：

```javascript
return {
  success: false,
  message: '未查询到标段信息'
};
```

平台包装后，MDF 常见收到：

```json
{
  "code": 200,
  "message": "操作成功",
  "data": {
    "success": true,
    "sectionCode": "CG260609001/001"
  }
}
```

因此协同 MDF 时必须说明：业务字段从 `res.data` 读取，例如 `res.data.sectionCode`。

---

## 4. 信息充分性门禁

生成后端 API 前必须确认：

| 要素 | 要求 |
|---|---|
| 查询对象 uri | 来自元数据，禁止猜 |
| domain | 来自元数据 `_entityInfo.domain` 或可信索引 |
| 查询条件字段 | 通常为 `id`，必须来自业务对象字段 |
| 返回字段 | 例如 `code/sectionCode/bbcode`，必须来自元数据或用户确认 |
| 前端回调样例 | 有样例按样例；无样例按平台包装说明并标待实测 |

任一字段不充分时，输出中标注待确认，不得把猜测字段当事实。

---

## 5. 输出要求

交付时必须同时说明：

- API 脚本文件名和平台函数建议编码。
- API 入参结构。
- API return 结构。
- MDF 前端应按哪条路径解析，例如 `res.data.sectionCode`。
- 哪些元数据字段仍待确认。
