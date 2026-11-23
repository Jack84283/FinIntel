import { useState } from "react";
import { Button } from "@/components/ui/button";
import { useLocale } from "../localization";

export function EvidenceHash({ label, value }: { label: string; value: string }) {
  const { t } = useLocale();
  const [status, setStatus] = useState("");
  const copy = async () => {
    try {
      await navigator.clipboard.writeText(value);
      setStatus("Copied");
    } catch {
      setStatus("Clipboard unavailable. Select the hash to copy it.");
    }
  };
  return <div className="grid gap-1 border-t py-2">
    <span>{label}</span>
    <code className="select-all break-all">{value}</code>
    <div><Button type="button" size="sm" variant="outline" onClick={() => void copy()}>{t("Copy hash")}</Button>
      <span className="ml-2" role="status">{status && t(status)}</span></div>
  </div>;
}
