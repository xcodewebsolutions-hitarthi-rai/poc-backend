# MDD规则链赋值

通过 `.set("字段编码", 值)` 在规则链脚本中修改单据数据。布尔类型和整数类型需加引号。

```javascript
// 后端函数规则链赋值
// 如果遇到赋值不成功的情况可以使用：param.data[0].set("字段编码",字段值+"")；
var AbstractTrigger = require('AbstractTrigger');
class MyTrigger extends AbstractTrigger {
  execute(context, param) {
    //层级0 BillDataDto
    param.set("key", "value")
    //层级2 主表数据
    param.data[0].set("key", "value");
    //层级3 主表自定义项
    param.data[0].headFreeItem.set("key", "value");
    //层级4 子表数据
    param.data[0].details[0].set("key", "value");
    //布尔类型、整数类型的需要加引号
    param.data[0].details[0].set('isParticipateRebate', 'false');
    //层级5 子表数据自定义项
    param.data[0].details[0].headFreeItem.set("key", "value");
    //层级6
    param.data[0].details[0].son[0].set("key", "value");
    //层级7
    param.data[0].details[0].son[0].mater.set("key", "value");
    param.data[0].set('time', '12:49:00');
    param.data[0].set('dateTime', '2024-08-22 12:49:00');
    param.data[0].set('date', '2024-08-22');
    //单据转换规则
    var omakeList = param.omake;
    //修改omakeList后set回去
    param.set('omake', JSON.stringify(omakeList));
    if (param.data[0].headFreeItem == undefined) {
      param.data[0].set("headFreeItem", {});
      param.data[0].headFreeItem.set("_entityName", "voucher.order.OrderFreeDefine");
      param.data[0].headFreeItem.set("key", value);
      param.data[0].headFreeItem.set("_status", "Update");
    } else {
      param.data[0].headFreeItem.set("key", value);
      param.data[0].headFreeItem.set("_status", "Update");
    }
    //set值记录
    if (typeof updateContext !== 'undefined' && updateContext && updateContext.logs) {
      console.error(JSON.stringify(updateContext.logs));
    }
    return { param: param };
  }
}
exports({ "entryPoint": MyTrigger });
```
