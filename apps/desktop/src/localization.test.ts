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

  it("preserves the default English implementation text", () => {
    expect(translate("en", "Overview")).toBe("Overview");
    expect(translate("en", "Market")).toBe("Market");
  });
});
