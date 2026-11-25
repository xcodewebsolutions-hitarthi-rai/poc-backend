# 核心API速查

> 本文档是 SKILL.md 的核心 API 参考表。
> 详细文档请见各模块文档的链接。

---

## 目录

- [元数据使用规范](#元数据使用规范强制)
- [模板与绑定](#模板与绑定)
- [数据查询](#数据查询)
- [实体操作](#实体操作)
- [远程调用](#远程调用)
- [上下文](#上下文)
- [加密解密](#加密解密)
- [字符串处理](#字符串处理)
- [金额计算](#金额计算)

---

## 元数据使用规范（强制）

> **写任何 YonSQL 或实体操作前，先判断 `uri/domain/字段/条件/字段类型` 是否充分；不足时才调用 `iuap-c-metadata-info`。**

| 操作元素 | 必须从元数据获取 |
|---------|----------------|
| **表名/URI** | `entities[].uri`（如 `pc.cls.ManagementClass`） |
| **字段名** | `entities[].attributes[].name` |
| **domainKey** | `entities[].domain` |
| **字段类型** | 基本类型 / 参照 / 特征 / 子表 / 平行表 / 多选引用 |

| ❌ 禁止 | ✅ 正确 |
|--------|--------|
| 使用 `tableName` | 使用 `uri` |
| 使用 `dbColumnName` | 使用 `name` |
| 臆想 domainKey | 使用元数据的 `domain` |
| **臆想任何字段**（如 `dr`、`iDeleted`、`pubts` 等） | **所有字段必须来自元数据返回的 `attributes`** |

---

## 模板与绑定

| 用途 | 文档 |
|------|------|
| 脚本模板（API/Rule） | `../10-common/01_代码框架.md` |
| MDD规则链入参 | `../20-input/01_MDD规则链入参.md` |
| YPD扩展点入参 | `../20-input/02_YPD扩展点入参.md` |
| 脚本绑定方式 | `../40-guides/02_后端脚本绑定指南.md` |

---

## 数据查询

> 详细语法见 `../10-common/02_YonQL语句.md`

| API | 说明 |
|-----|------|
| `ObjectStore.queryByYonQL(sql)` | YonSQL 查询（本领域） |
| `ObjectStore.queryByYonQL(sql, domainKey)` | YonSQL 查询（跨领域） |
| `ObjectStore.selectById(uri, object)` | 按ID查询，支持组合实体 |
| `ObjectStore.selectByMap(uri, object)` | 按条件查询 |

### 快速示例

```javascript
// YonSQL 查询
var sql = `select code, name from pc.cls.ManagementClass where dr = 0`;
var result = ObjectStore.queryByYonQL(sql, 'productcenter');

// 按ID查询
var object = { id: 'targetId' };
var res = ObjectStore.selectById('entity.uri', object);
```

---

## 实体操作

> 详细语法见 `../10-common/06_实体操作.md`

| API | 说明 |
|-----|------|
| `ObjectStore.insert(uri, data, billNo)` | 插入实体 |
| `ObjectStore.insertBatch(uri, dataArray, billNo)` | 批量插入 |
| `ObjectStore.updateById(uri, data, billNo)` | 按ID更新 |
| `ObjectStore.update(uri, data, wrapper, billNo)` | 按条件更新 |
| `ObjectStore.deleteById(uri, object, billNo)` | 按ID删除 |
| `ObjectStore.deleteByMap(uri, object, billNo)` | 按条件删除 |

### 快速示例

```javascript
// 插入
var newRecord = { name: '测试', code: 'TEST001' };
var res = ObjectStore.insert('entity.uri', newRecord, 'billNo');

// 更新
var toUpdate = { id: 'xxx', name: '新名称' };
var res = ObjectStore.updateById('entity.uri', toUpdate, 'billNo');
```

> 子表通过 `_status` 控制：`Insert`（新增）、`Update`（修改）、`Delete`（删除）

---

## 远程调用

> 详细语法见 `../10-common/05_远程调用.md`

| API | 说明 |
|-----|------|
| `ObjectStore.openLinker(method, url, appCode, body)` | 友企联接口（**BIP内部**，自动认证） |
| `ObjectStore.openLinkerPure(method, url, appCode, body)` | 友企联接口（appKey/appSecret身份） |
| `ObjectStore.postman(method, url, header, body)` | REST接口（**跨租户/三方**） |
| `ObjectStore.nccLinker(method, url, header, body, nccEnv)` | NCC接口 |
| `ObjectStore.apiman(method, url, header, body)` | APIlink接口 |
| `extrequire(scriptCode).execute(params)` | 调用其他后端脚本 |

### 调用决策

| 场景 | 调用方式 | 认证 |
|------|---------|------|
| **BIP内部API（本租户）** | `openLinker` | 自动 |
| **跨租户/三方API** | `postman` | 手动 |
| **NCC接口** | `nccLinker` | nccEnv |
| **其他脚本** | `extrequire` | 自动 |

### 快速示例

```javascript
// BIP内部API（【规范3】使用 openLinker）
var url = '/yonbip/xxx/xxx';
var resultString = ObjectStore.openLinker('POST', url, 'appCode', body);
var result = safeJsonParse(resultString, { longFields: [], lengthThreshold: 16 });

// 跨租户/三方（【规范4】使用 postman）
var header = { 'Content-Type': 'application/json' };
var resultString = ObjectStore.postman('POST', url, header, body);
var result = safeJsonParse(resultString, { longFields: [], lengthThreshold: 16 });
```

---

## 上下文

| API | 说明 |
|-----|------|
| `ObjectStore.user()` | 用户信息：id, name, tenantId, staffId |
| `ObjectStore.env()` | 环境信息：url, tenantId, token, wb_at |
| `ObjectStore.env('ymskey')` | 获取YMS配置值 |

### 快速示例

```javascript
var env = ObjectStore.env();
var url = env.url + '/iuap-api-gateway/...';  // 获取API地址
var tenantId = env.tenantId;  // 获取租户ID
```

---

## 加密解密

> 详细语法见 `../10-common/08_加密解密.md`

| API | 说明 |
|-----|------|
| `MD5Encode(text)` | MD5加密 |
| `SHA256Encode(text)` | SHA256加密 |
| `Base64Encode(text)` / `Base64Decode(text)` | Base64编码/解码 |
| `AESEncrypt(text, key, iv)` / `AESDecrypt(text, key, iv)` | AES加密/解密 |
| `HmacSHA256(text, key)` | HmacSHA256加密 |

---

## 字符串处理

> 详细语法见 `../10-common/09_字符串处理.md`

| API | 说明 |
|-----|------|
| `replace(text, pattern, replacement)` | 替换字符 |
| `split(text, separator)` | 分割字符串 |
| `trim(text)` | 去除首尾空格 |
| `UrlEncode(url)` / `UrlDecode(url)` | URL编码/解码 |
| `json2string(obj)` | 对象转JSON字符串 |
| `string2json(str)` | JSON字符串转对象 |

---

## 金额计算

> 详细语法见 `../10-common/12_金额计算.md`

| API | 说明 |
|-----|------|
| `GetBigDecimal(value)` | 转BigDecimal |
| `moneyFormat(value, scale)` | 四舍五入格式化金额 |
| `ObjectStore.moneyFormat(value)` | 格式化金额 |

---

## 消息触达

> 详细语法见 `../30-scenes/02_消息触达.md`

| API | 说明 |
|-----|------|
| `sendMessage(messageInfo)` | 发送消息（邮件/友空间/短信） |

参数：`sysId`、`tenantId`、`channels`(mail/uspace/sms)、`subject`(标题)、`content`(内容)
