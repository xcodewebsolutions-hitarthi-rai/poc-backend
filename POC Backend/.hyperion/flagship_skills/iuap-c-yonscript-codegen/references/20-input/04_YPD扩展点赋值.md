# YPD扩展点赋值

通过 `.set("字段编码", 值)` 在YPD扩展点脚本中修改单据数据。

```javascript
// 后端函数ypd扩展点赋值
// 如果遇到赋值不成功的情况可以使用：param.billDOs[0].set("字段编码",字段名+"")；
var AbstractTrigger = require('AbstractTrigger');
class MyTrigger extends AbstractTrigger {
  execute(context, param) {
    var apPaymentApplyHExt = param.billDOs[0].extendData.apPaymentApplyHExt;

    if (apPaymentApplyHExt != null) {
      apPaymentApplyHExt.set("approverOne", "2797727616831768");
    } else {
      param.billDOs[0].extendData.apPaymentApplyHExt = {};
      param.billDOs[0].extendData.apPaymentApplyHExt.set("_fullName", "A.B.C");
      param.billDOs[0].extendData.apPaymentApplyHExt.set("approverOne", "2797727616831768");
    }

    param.billDOs[0].set("remarks", "备注");

    param.billDOs[0].extendData.set("approverOne", "2797727616831768");
    param.billDOs[0].extendData.set("approverOne_name", "宋");

    param.billDOs[0].detailList[0].set("name", "宋");
    param.billDOs[0].detailList[0].extendData.set("name", "宋");

    if (typeof updateContext !== 'undefined' && updateContext && updateContext.logs) {
      console.error(JSON.stringify(updateContext.logs));
    }
    return {};
  }
}
exports({ "entryPoint": MyTrigger });
```
