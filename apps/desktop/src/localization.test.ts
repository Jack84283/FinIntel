import { describe, expect, it } from "vitest";
import { translate, type TranslationKey } from "./localization";

describe("English and Simplified Chinese messages", () => {
  it("translates navigation labels without changing code identifiers", () => {
    expect(translate("en", "Index Constituents")).toBe("Index Constituents");
    expect(translate("zh-CN", "Index Constituents")).toBe("指数成分股");
    expect(translate("zh-CN", "Data Acquisition")).toBe("数据采集");
  });

  it("translates Preferences and remains deterministic", () => {
    const key: TranslationKey = "Select the interface language.";
    expect(translate("zh-CN", key)).toBe("选择界面语言。");
    expect(translate("en", key)).toBe("Select the interface language.");
    expect(translate("zh-CN", "Privacy & storage")).toBe("隐私与存储");
  });

  it("translates the five reported live control and table labels", () => {
    const expected = {
      "Download history": "下载历史数据",
      "Verify corporate events": "验证公司行为",
      "Check indexes": "检查指数",
      "Fetch constituents": "获取成分股",
      "Session": "交易日",
    };
    for (const [key, chinese] of Object.entries(expected)) {
      expect(translate("zh-CN", key)).toBe(chinese);
    }
  });

  it("preserves the default English implementation text", () => {
    expect(translate("en", "Overview")).toBe("Overview");
    expect(translate("en", "Market")).toBe("Market");
  });
});
