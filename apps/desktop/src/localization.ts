import { readWorkspacePreferences, type AppLanguage } from "./workspace-preferences";

const translations = {
  en: {
    Workspace: "Workspace", "Data & Operations": "Data & Operations",
    "Workspace & Preferences": "Workspace & Preferences",
    Overview: "Overview", Market: "Market", Intelligence: "Intelligence",
    "Data Acquisition": "Data Acquisition", "Data Validation": "Data Validation",
    "Index Constituents": "Index Constituents", "Action History": "Action History",
    Portfolio: "Portfolio", Preferences: "Preferences",
    "PRIVATE · LOCAL FIRST": "PRIVATE · LOCAL FIRST",
    "Private Financial Intelligence": "Private Financial Intelligence",
    "Explore locally stored market data and build your private research workspace.": "Explore locally stored market data and build your private research workspace.",
    "Historical research": "Historical research", "Your local market workspace.": "Your local market workspace.",
    "Open Market Data Explorer": "Open Market Data Explorer", "Open Data Acquisition": "Open Data Acquisition",
    "Python backend": "Python backend", "Research foundations": "Research foundations",
    "Local data first, quant intelligence later.": "Local data first, quant intelligence later.",
    "Historical data acquisition": "Historical data acquisition", "SQLite persistence": "SQLite persistence",
    "Market Data Explorer": "Market Data Explorer", Implemented: "Implemented",
    "In review": "In review", "Recheck connection": "Recheck connection",
    Connected: "Connected", Unavailable: "Unavailable", "Checking…": "Checking…",
    "Not implemented": "Not implemented", "Coming in a future milestone": "Coming in a future milestone",
    "This is a navigation placeholder. No live data or actions are available on this page yet.": "This is a navigation placeholder. No live data or actions are available on this page yet.",
    "Proprietary models, local AI, and market trend analysis will appear here.": "Proprietary models, local AI, and market trend analysis will appear here.",
    "Holdings, performance, allocation, and risk exposure will appear here.": "Holdings, performance, allocation, and risk exposure will appear here.",
    "Use Data Acquisition to fetch prices and review completed import actions. Open Markets to explore stored OHLCV charts, historical tables, and instrument-level source provenance.": "Use Data Acquisition to fetch prices and review completed import actions. Open Markets to explore stored OHLCV charts, historical tables, and instrument-level source provenance.",
    "Appearance": "Appearance", "Color theme": "Color theme", "Dark": "Dark", "Light": "Light",
    "Reduce motion": "Reduce motion", Enabled: "Enabled", Disabled: "Disabled",
    "Language": "Language", "English": "English", "简体中文": "简体中文",
    "Select the interface language.": "Select the interface language.",
    "Historical market data": "Historical market data", "Privacy & storage": "Privacy & storage",
    "Local backend": "Local backend", "Device-local": "Device-local",
  },
  "zh-CN": {
    Workspace: "工作区", "Data & Operations": "数据与运维",
    "Workspace & Preferences": "工作区与偏好设置",
    Overview: "概览", Market: "市场", Intelligence: "智能分析",
    "Data Acquisition": "数据采集", "Data Validation": "数据校验",
    "Index Constituents": "指数成分股", "Action History": "操作记录",
    Portfolio: "投资组合", Preferences: "偏好设置",
    "PRIVATE · LOCAL FIRST": "私有 · 本地优先",
    "Private Financial Intelligence": "私人金融智能平台",
    "Explore locally stored market data and build your private research workspace.": "探索本地市场数据，建立专属研究工作区。",
    "Historical research": "历史数据研究", "Your local market workspace.": "您的本地市场研究工作区。",
    "Open Market Data Explorer": "打开市场数据浏览器", "Open Data Acquisition": "打开数据采集",
    "Python backend": "Python 后端", "Research foundations": "研究基础",
    "Local data first, quant intelligence later.": "先建设本地数据基础，再扩展量化智能。",
    "Historical data acquisition": "历史数据采集", "SQLite persistence": "SQLite 数据存储",
    "Market Data Explorer": "市场数据浏览器", Implemented: "已实现",
    "In review": "审核中", "Recheck connection": "重新检查连接",
    Connected: "已连接", Unavailable: "不可用", "Checking…": "正在检查…",
    "Not implemented": "尚未实现", "Coming in a future milestone": "将在后续版本提供",
    "This is a navigation placeholder. No live data or actions are available on this page yet.": "此页面仍在规划中，尚无可用数据或操作。",
    "Proprietary models, local AI, and market trend analysis will appear here.": "此处将提供专有模型、本地 AI 与市场趋势分析。",
    "Holdings, performance, allocation, and risk exposure will appear here.": "此处将提供持仓、收益表现、资产配置与风险敞口。",
    "Use Data Acquisition to fetch prices and review completed import actions. Open Markets to explore stored OHLCV charts, historical tables, and instrument-level source provenance.": "在数据采集页面获取价格并查看导入记录；在市场页面浏览本地行情图表、历史数据和来源信息。",
    "Appearance": "外观", "Color theme": "配色主题", "Dark": "深色", "Light": "浅色",
    "Reduce motion": "减少动态效果", Enabled: "已启用", Disabled: "已禁用",
    "Language": "语言", "English": "英语", "简体中文": "简体中文",
    "Select the interface language.": "选择界面语言。",
    "Historical market data": "历史市场数据", "Privacy & storage": "隐私与存储",
    "Local backend": "本地后端", "Device-local": "仅限本机",
  },
} as const;

export type TranslationKey = keyof typeof translations.en;
export function translate(language: AppLanguage, key: TranslationKey): string {
  return translations[language][key];
}
export function currentLanguage(): AppLanguage {
  return readWorkspacePreferences().language;
}
