# API 脚本查询与日志模式

> **加载时机**：生成前端 `invokeFunction` 调用的公有云 API 脚本，且脚本内涉及 YonSQL 查询、返回数据给前端、生产日志排查时。  
> **职责**：固化 API 脚本平台模板壳、YonSQL 查询安全、DateTime 字面量、生产日志和返回结构约定。  
> **边界**：不重复元数据门禁、OpenAPI 查询、事件订阅规则；URI/domain/字段来源仍回到治理层。

---

## 1. API 脚本模板壳不可变

公有云 API 脚本粘贴到后端函数设计器时，平台模板前三行和后三行必须保持不变。

前三行固定：

```javascript
let AbstractAPIHandler = require('AbstractAPIHandler');
class MyAPIHandler extends AbstractAPIHandler {
  execute(request) {
```

后三行固定：

```javascript
  }
}
exports({ entryPoint: MyAPIHandler });
```

禁止在前三行之前或后三行之后加入任何内容：注释、空行、变量、工具函数都不允许。业务逻辑、注释、局部函数全部写在 `execute(request)` 内部。

说明：API 脚本首行 `let AbstractAPIHandler` 是平台模板壳的一部分，不要改成 `var`，也不要在其前面加注释或空行。

---

## 2. 入参处理与 SQL 注入防护

前端、三方、可编辑字段传入的值进入 YonSQL 前必须先校验再转义。

ID 类参数推荐先做白名单：

```javascript
function normalizeId(value) {
  if (!value) return null;
  if (typeof value === 'string' || typeof value === 'number') return String(value);
  return value.id || value.value || value.pk || null;
}

function safeId(value) {
  var id = normalizeId(value);
  if (!id) return null;
  if (!/^[0-9A-Za-z_-]+$/.test(id)) {
    console.error('【脚本名】非法ID参数:' + id);
    return null;
  }
  return id;
}

function quoteYonQL(value) {
  return "'" + String(value).replace(/'/g, "''") + "'";
}
```

字段名、排序字段、URI、domain 只能来自元数据或固定白名单，不允许来自 request。

---

## 3. DateTime 字面量

YonSQL 中不要生成 `validtime > now()`。平台会把 `now()` 当字符串，导致 DateTime 转换失败。

推荐在 JS 中生成标准时间字符串再拼入 YonSQL：

```javascript
function getCurrentDateTime() {
  var date = new Date();
  var pad = function (num) {
    return num < 10 ? '0' + num : num;
  };
  return date.getFullYear() + '-'
    + pad(date.getMonth() + 1) + '-'
    + pad(date.getDate()) + ' '
    + pad(date.getHours()) + ':'
    + pad(date.getMinutes()) + ':'
    + pad(date.getSeconds());
}
```

YonSQL 条件写法：

```javascript
var nowDate = getCurrentDateTime();
var sql = `select id
from cpubase.supplycategory.Supplycategory
where validtime is null or validtime > ${quoteYonQL(nowDate)}`;
```

---

## 4. SQL 组织方式

允许使用模板字符串提高可读性，但插值位置必须只放经过校验/转义后的值。

```javascript
var sql = `select id, SupplycategoryDefineCharacter.Tmake as manufacturerId
from cpubase.supplycategory.Supplycategory
where purchaseOrgId = ${quoteYonQL(purchaseOrgId)}
  and supplyDocId = ${quoteYonQL(vendorId)}
  and materialId = ${quoteYonQL(materialId)}
  and status = ${quoteYonQL('TOSHELF')}
  and (validtime is null or validtime > ${quoteYonQL(nowDate)})`;
```

禁止事项：

- 禁止 `select *`。
- 禁止把 request 原值直接放进模板字符串。
- 禁止用底层表名或 dbColumnName 组织 YonSQL。
- 禁止把参照、特征、子表、平行表字段按基本字段直接写；先回到字段命名契约确认类型。
- 禁止为了“通用”加入未经验证的 `cast(...)`、数据库函数或原生 SQL 方言。

---

## 5. 结果处理

返回 ID 列表时推荐用 `Set` 去重，不要手写 `exists` 对象。

```javascript
var ids = Array.from(new Set(
  result
    .map(function (item) { return normalizeId(item && item.manufacturerId); })
    .filter(function (id) { return id; })
));
```

返回给前端时保持简单结构：

```javascript
return { success: true, manufacturerIds: ids };
```

平台实际给前端 `invokeFunction` 回调时可能包装为：

```json
{
  "code": 200,
  "message": "操作成功",
  "data": {
    "success": true,
    "spendtime": 138,
    "manufacturerIds": ["2391624810863001600"]
  }
}
```

前端 MDF 片段应按真实平台结构读取，例如 `resp.data.manufacturerIds`，不要为未知结构堆多层兼容分支。

---

## 6. 生产日志

公有云生产环境通常不能调试，API 脚本应保留关键 `console.error`。但不要打印大对象完整 JSON，避免日志过大和性能问题。

推荐打印：

- 入参关键 ID。
- 入参缺失或非法 ID。
- 查询 SQL。
- 查询结果行数。
- 返回 ID 数量和必要的 ID 列表。

推荐写法：

```javascript
console.error('【脚本名】入参ID purchaseOrgId=' + purchaseOrgId + ', vendorId=' + vendorId + ', materialId=' + materialId);
console.error('【脚本名】查询SQL:' + sql);
console.error('【脚本名】查询结果行数:' + result.length);
console.error('【脚本名】返回ID数量:' + ids.length + ', ids=' + ids.join(','));
```

禁止默认打印：

```javascript
console.error(JSON.stringify(result));
```

大对象日志只有在用户明确要求排查某个问题时临时加入，交付前应删除或降级为数量/关键字段日志。

---

## 7. 生成检查清单

- [ ] API 脚本前三行和后三行是否与平台模板完全一致。
- [ ] 模板外是否没有注释、空行、变量、函数。
- [ ] 所有业务逻辑和工具函数是否都在 `execute(request)` 内。
- [ ] request 入参进入 YonSQL 前是否先白名单/转义。
- [ ] URI/domain/字段是否来自元数据或固定白名单。
- [ ] DateTime 比较是否没有使用 `now()` 字面量。
- [ ] YonSQL 是否没有 `select *` 和底层字段名。
- [ ] 涉及参照/特征/子表/平行表时是否已按字段类型选择写法。
- [ ] 日志是否满足生产可诊断且未打印大对象。
- [ ] 返回结构是否简单，并说明前端平台包装后的读取方式。
