/** Independent client integrity check of trusted, versioned local model output.
 *
 * Python uses decimal.Context(prec=28, rounding=ROUND_HALF_EVEN) for *each*
 * addition in sum(window), then for the division by window. BigInt integers
 * retain exact decimal digits here and reproduce that boundary without
 * binary floating point or an additional dependency.
 */
import type { ModelResearchPoint } from "./model-research-api";

type Exact = { coefficient: bigint; scale: number };
const pow10 = (places: number): bigint => {
  if (!Number.isSafeInteger(places) || places < 0 || places > 512) {
    throw new Error("Model decimal precision is outside supported bounds.");
  }
  return 10n ** BigInt(places);
};
function parseExact(value: string): Exact {
  if (value.length > 200 || !/^\d+(?:\.\d+)?$/.test(value)) {
    throw new Error("Invalid model decimal precision.");
  }
  const [whole, fractional = ""] = value.split(".");
  return { coefficient: BigInt(whole + fractional), scale: fractional.length };
}
function halfEven(numerator: bigint, denominator: bigint): bigint {
  const quotient = numerator / denominator;
  const remainder = numerator % denominator;
  return quotient + (
    remainder * 2n > denominator ||
    (remainder * 2n === denominator && quotient % 2n === 1n) ? 1n : 0n
  );
}
function round28(value: Exact): Exact {
  if (value.coefficient === 0n) return value;
  const extra = value.coefficient.toString().length - 28;
  if (extra <= 0) return value;
  return {
    coefficient: halfEven(value.coefficient, pow10(extra)),
    scale: value.scale - extra,
  };
}
function sum28(values: Exact[]): Exact {
  let sum: Exact = { coefficient: 0n, scale: 0 };
  for (const value of values) {
    const scale = Math.max(sum.scale, value.scale);
    sum = round28({
      coefficient: sum.coefficient * pow10(scale - sum.scale) +
        value.coefficient * pow10(scale - value.scale),
      scale,
    });
  }
  return sum;
}
function divide28(value: Exact, window: number): Exact {
  const numerator = value.scale >= 0
    ? value.coefficient : value.coefficient * pow10(-value.scale);
  const denominator = BigInt(window) * (value.scale >= 0 ? pow10(value.scale) : 1n);
  if (numerator === 0n) return { coefficient: 0n, scale: 0 };

  // Determine floor(log10(numerator/denominator)) using exact comparisons.
  let exponent = numerator.toString().length - denominator.toString().length;
  if (exponent >= 0) {
    if (numerator < denominator * pow10(exponent)) exponent--;
  } else if (numerator * pow10(-exponent) < denominator) {
    exponent--;
  }
  const places = 27 - exponent; // 28 significant decimal digits
  return places >= 0
    ? { coefficient: halfEven(numerator * pow10(places), denominator), scale: places }
    : { coefficient: halfEven(numerator, denominator * pow10(-places)), scale: places };
}
function equalExact(expected: Exact, value: string | null): boolean {
  if (value === null) return false;
  const actual = parseExact(value);
  const scale = Math.max(expected.scale, actual.scale);
  return expected.coefficient * pow10(scale - expected.scale) ===
    actual.coefficient * pow10(scale - actual.scale);
}
export function verifyIndicatorAverages(
  points: ModelResearchPoint[], fastWindow: number, slowWindow: number,
): void {
  const levels = points.map(point => parseExact(point.close));
  for (let i = 0; i < points.length; i++) {
    for (const [window, column] of [
      [fastWindow, "fast_sma"],
      [slowWindow, "slow_sma"],
    ] as const) {
      if (i + 1 < window) {
        if (points[i][column] !== null) {
          throw new Error("Model SMA warm-up includes future observations.");
        }
        continue;
      }
      const windowSum = sum28(levels.slice(i + 1 - window, i + 1));
      const average = divide28(windowSum, window);
      if (!equalExact(average, points[i][column])) {
        throw new Error("Model SMA does not match actual trailing observed closes.");
      }
    }
  }
}

/** The exact canonical SHA-256 input used by Python json.dumps(sort_keys=True,
 * ensure_ascii=True, separators=(",", ":")). Neither timestamps nor any URL
 * are added to the source-provided lineage or output arrays.
 */
function canonicalJson(value: unknown): string {
  if (Array.isArray(value)) return "[" + value.map(canonicalJson).join(",") + "]";
  if (value !== null && typeof value === "object") {
    const object = value as Record<string, unknown>;
    return "{" + Object.keys(object).sort().map(key =>
      canonicalJson(key) + ":" + canonicalJson(object[key])
    ).join(",") + "}";
  }
  const literal = JSON.stringify(value);
  if (literal === undefined) throw new Error("Unsupported canonical model value.");
  // Python's ensure_ascii=True escapes non-ASCII code units, including surrogates.
  return literal.replace(/[\u007f-\uffff]/g, char =>
    "\\u" + char.charCodeAt(0).toString(16).padStart(4, "0")
  );
}
export async function canonicalModelSha256(value: unknown): Promise<string> {
  if (!globalThis.crypto?.subtle) {
    throw new Error("Cryptographic verification is unavailable; refusing model output.");
  }
  const bytes = new TextEncoder().encode(canonicalJson(value));
  const result = await globalThis.crypto.subtle.digest("SHA-256", bytes);
  return Array.from(new Uint8Array(result)).map(byte => byte.toString(16).padStart(2, "0")).join("");
}
