import { describe, expect, it } from "vitest";
import { renderToStaticMarkup } from "react-dom/server";
import { Button, buttonVariants } from "./button";
import { Badge, badgeVariants } from "./badge";
import { Card, CardContent, CardHeader, CardTitle } from "./card";
import { Skeleton } from "./skeleton";
import { cn } from "@/lib/utils";

describe("source-owned shadcn/ui primitives", () => {
  it("renders semantic card and badge slots with FI variants", () => {
    const markup = renderToStaticMarkup(
      <Card>
        <CardHeader><CardTitle>FinIntel</CardTitle></CardHeader>
        <CardContent><Badge variant="outline">Local API</Badge></CardContent>
      </Card>
    );
    expect(markup).toContain('data-slot="card"');
    expect(markup).toContain('data-slot="card-title"');
    expect(markup).toContain('data-slot="badge"');
    expect(markup).toContain("Local API");
  });

  it("renders a Base UI button with its event-safe button semantics", () => {
    const markup = renderToStaticMarkup(<Button type="button" variant="secondary">Retry</Button>);
    expect(markup).toContain('data-slot="button"');
    expect(markup).toContain("Retry");
  });

  it("provides stable design variants and Tailwind utility merging", () => {
    expect(buttonVariants({ variant: "secondary" })).toContain("bg-secondary");
    expect(badgeVariants({ variant: "success" })).toContain("text-positive");
    expect(cn("px-2", "px-4")).toBe("px-4");
    expect(renderToStaticMarkup(<Skeleton />)).toContain('data-slot="skeleton"');
  });
});
