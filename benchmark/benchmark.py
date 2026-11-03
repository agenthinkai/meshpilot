#!/usr/bin/env python3
"""
MeshPilot Benchmark Script
==========================
Real, live comparison of MeshPilot (llama.cpp, INT4) vs. plain PyTorch (FP32)
CPU inference — same model, same prompts, same machine, every number measured
against a running MeshPilot instance. There is no simulated/offline mode: this
script only ever reports what the server actually measured.

Usage:
    python benchmark.py --meshpilot-url http://localhost:8100 --api-key mp_your_key
    python benchmark.py --meshpilot-url http://localhost:8100 --api-key mp_your_key --iterations 5
"""

import argparse
import json
import os
import platform
import statistics
import sys
import time
from dataclasses import dataclass
from datetime import datetime
from typing import List, Optional

import httpx

# ── Dataclasses ───────────────────────────────────────────────────────────────

@dataclass
class BenchmarkResult:
    backend:           str
    model_name:        str
    quant:             str
    prompt_tokens:     int
    completion_tokens: int
    latency_ms:        List[float]
    throughput_tps:    List[float]
    memory_mb:         List[float]
    cpu_threads:       int

    @property
    def p50_latency(self) -> float:
        return statistics.median(self.latency_ms)

    @property
    def p90_latency(self) -> float:
        s = sorted(self.latency_ms)
        idx = int(len(s) * 0.9)
        return s[min(idx, len(s) - 1)]

    @property
    def p99_latency(self) -> float:
        s = sorted(self.latency_ms)
        idx = int(len(s) * 0.99)
        return s[min(idx, len(s) - 1)]

    @property
    def avg_throughput(self) -> float:
        return statistics.mean(self.throughput_tps) if self.throughput_tps else 0.0

    @property
    def p90_throughput(self) -> float:
        s = sorted(self.throughput_tps, reverse=True)
        idx = int(len(s) * 0.1)
        return s[min(idx, len(s) - 1)]

    @property
    def avg_memory_mb(self) -> float:
        return statistics.mean(self.memory_mb) if self.memory_mb else 0.0


@dataclass
class BenchmarkReport:
    timestamp:           str
    platform:            str
    cpu_model:           str
    cpu_cores:           int
    ram_gb:              float
    iterations:          int
    prompt:              str
    pytorch_cpu:         Optional[BenchmarkResult]
    meshpilot:           Optional[BenchmarkResult]
    speedup_p50:         Optional[float]
    speedup_p90:         Optional[float]
    memory_saving_pct:   Optional[float]
    throughput_gain_pct: Optional[float]


# ── Live benchmark runner ──────────────────────────────────────────────────────

def fetch_model_name(url: str, api_key: str, model_id: str) -> str:
    """Look up the real model name from the live server instead of hardcoding it."""
    try:
        resp = httpx.get(f"{url}/api/v1/models", headers={"X-API-Key": api_key}, timeout=15)
        resp.raise_for_status()
        for m in resp.json():
            if m.get("id") == model_id:
                return m.get("name", model_id)
    except Exception:
        pass
    return model_id


def run_compare_benchmark(
    url: str, api_key: str, model_id: str,
    prompt: str, max_tokens: int, iterations: int,
) -> "tuple[BenchmarkResult, BenchmarkResult]":
    """Calls the live MeshPilot /inference/compare/{model_id} endpoint N times.
    Every value collected here is a real measurement the server made — the
    same code path that runs both the llama.cpp (MeshPilot) side and the
    plain PyTorch baseline side concurrently and reports real latency,
    throughput, and live container memory usage for each."""
    model_name = fetch_model_name(url, api_key, model_id)
    headers = {"X-API-Key": api_key, "Content-Type": "application/json"}

    mesh_latency, mesh_tps, mesh_mem = [], [], []
    pt_latency, pt_tps, pt_mem = [], [], []
    completion_tokens = max_tokens

    print(f"\n[Compare] Running {iterations} live iterations against {url} ...")
    for i in range(iterations):
        try:
            resp = httpx.post(
                f"{url}/api/v1/inference/compare/{model_id}",
                headers=headers,
                json={"prompt": prompt, "max_tokens": max_tokens, "temperature": 0.0},
                timeout=180,
            )
            resp.raise_for_status()
            data = resp.json()
            w, wo = data["with_meshpilot"], data["without_meshpilot"]
            if w.get("error") or wo.get("error"):
                print(f"\n  Warning: iteration {i+1} returned an error: "
                      f"{w.get('error') or wo.get('error')}")
                continue

            mesh_latency.append(w["wall_clock_ms"])
            mesh_tps.append(w["throughput_tps"])
            if w.get("memory_mb") is not None:
                mesh_mem.append(w["memory_mb"])
            completion_tokens = w.get("completion_tokens", completion_tokens)

            pt_latency.append(wo["wall_clock_ms"])
            pt_tps.append(wo["throughput_tps"])
            if wo.get("memory_mb") is not None:
                pt_mem.append(wo["memory_mb"])

            sys.stdout.write(f"\r  Progress: {i+1}/{iterations} iterations")
            sys.stdout.flush()
        except Exception as e:
            print(f"\n  Warning: iteration {i+1} failed: {e}")

    print(f"\n  Completed {len(mesh_latency)} successful iterations")

    if not mesh_latency or not pt_latency:
        print("\nERROR: no successful iterations completed. Is MeshPilot running "
              "and reachable, and does the model support Compare mode?", file=sys.stderr)
        sys.exit(1)

    meshpilot_result = BenchmarkResult(
        backend="meshpilot_llamacpp_int4",
        model_name=f"{model_name} (Q4_K_M)",
        quant="INT4 (Q4_K_M)",
        prompt_tokens=0,
        completion_tokens=completion_tokens,
        latency_ms=mesh_latency,
        throughput_tps=mesh_tps,
        memory_mb=mesh_mem,
        cpu_threads=4,
    )
    pytorch_result = BenchmarkResult(
        backend="pytorch_cpu_fp32",
        model_name=f"{model_name} (FP32)",
        quant="FP32",
        prompt_tokens=0,
        completion_tokens=completion_tokens,
        latency_ms=pt_latency,
        throughput_tps=pt_tps,
        memory_mb=pt_mem,
        cpu_threads=4,
    )
    return pytorch_result, meshpilot_result


# ── Report generation ─────────────────────────────────────────────────────────

def get_system_info() -> dict:
    import multiprocessing
    try:
        import psutil
        ram_gb = round(psutil.virtual_memory().total / 1024**3, 1)
    except ImportError:
        ram_gb = 0.0

    try:
        cpu_model = platform.processor() or "Unknown CPU"
    except Exception:
        cpu_model = "Unknown CPU"

    return {
        "platform": platform.platform(),
        "cpu_model": cpu_model,
        "cpu_cores": multiprocessing.cpu_count(),
        "ram_gb": ram_gb,
    }


def print_comparison_table(pytorch: BenchmarkResult, meshpilot: BenchmarkResult):
    speedup_p50 = pytorch.p50_latency / meshpilot.p50_latency
    speedup_p90 = pytorch.p90_latency / meshpilot.p90_latency
    mem_saving = (
        (1 - meshpilot.avg_memory_mb / pytorch.avg_memory_mb) * 100
        if pytorch.avg_memory_mb and meshpilot.avg_memory_mb else None
    )
    tps_gain = (meshpilot.avg_throughput / pytorch.avg_throughput - 1) * 100

    w = 60
    print("\n" + "=" * w)
    print("  MESHPILOT BENCHMARK RESULTS (live, real inference)".center(w))
    print("=" * w)
    print(f"  {'Metric':<28} {'PyTorch FP32':>12} {'MeshPilot INT4':>14}")
    print("-" * w)
    print(f"  {'Model':<28} {pytorch.model_name:>12} {meshpilot.model_name:>14}")
    print(f"  {'Quantization':<28} {'FP32':>12} {'INT4 Q4_K_M':>14}")
    print(f"  {'Backend':<28} {'PyTorch':>12} {'llama.cpp':>14}")
    if mem_saving is not None:
        print(f"  {'Memory (MB, live-measured)':<28} {pytorch.avg_memory_mb:>12.0f} {meshpilot.avg_memory_mb:>14.0f}")
    print("-" * w)
    print(f"  {'P50 Latency (ms)':<28} {pytorch.p50_latency:>12.1f} {meshpilot.p50_latency:>14.1f}")
    print(f"  {'P90 Latency (ms)':<28} {pytorch.p90_latency:>12.1f} {meshpilot.p90_latency:>14.1f}")
    print(f"  {'P99 Latency (ms)':<28} {pytorch.p99_latency:>12.1f} {meshpilot.p99_latency:>14.1f}")
    print(f"  {'Avg Throughput (t/s)':<28} {pytorch.avg_throughput:>12.1f} {meshpilot.avg_throughput:>14.1f}")
    print(f"  {'P90 Throughput (t/s)':<28} {pytorch.p90_throughput:>12.1f} {meshpilot.p90_throughput:>14.1f}")
    print("-" * w)
    print(f"  {'P50 Speedup':<28} {'1.00x':>12} {speedup_p50:>13.2f}x")
    print(f"  {'P90 Speedup':<28} {'1.00x':>12} {speedup_p90:>13.2f}x")
    if mem_saving is not None:
        print(f"  {'Memory Reduction':<28} {'0%':>12} {mem_saving:>13.1f}%")
    print(f"  {'Throughput Gain':<28} {'0%':>12} {tps_gain:>13.1f}%")
    print("=" * w)
    print(f"\n  MeshPilot delivered {speedup_p50:.1f}x faster median inference (measured, not simulated)")
    if mem_saving is not None:
        print(f"  {mem_saving:.0f}% less memory, live-sampled via Docker stats during inference")
    print(f"  {tps_gain:.0f}% higher throughput on the same CPU hardware")
    print()

    return speedup_p50, speedup_p90, mem_saving, tps_gain


def save_report(report: BenchmarkReport, output_dir: str = "."):
    os.makedirs(output_dir, exist_ok=True)

    json_path = os.path.join(output_dir, "benchmark_results.json")
    report_dict = {
        "timestamp":           report.timestamp,
        "platform":            report.platform,
        "cpu_model":           report.cpu_model,
        "cpu_cores":           report.cpu_cores,
        "ram_gb":              report.ram_gb,
        "iterations":          report.iterations,
        "prompt":              report.prompt,
        "speedup_p50":         report.speedup_p50,
        "speedup_p90":         report.speedup_p90,
        "memory_saving_pct":   report.memory_saving_pct,
        "throughput_gain_pct": report.throughput_gain_pct,
        "pytorch_cpu": {
            "backend":            report.pytorch_cpu.backend,
            "quant":              report.pytorch_cpu.quant,
            "p50_latency_ms":     report.pytorch_cpu.p50_latency,
            "p90_latency_ms":     report.pytorch_cpu.p90_latency,
            "avg_throughput_tps": report.pytorch_cpu.avg_throughput,
            "avg_memory_mb":      report.pytorch_cpu.avg_memory_mb,
            "raw_latencies":      report.pytorch_cpu.latency_ms,
        } if report.pytorch_cpu else None,
        "meshpilot": {
            "backend":            report.meshpilot.backend,
            "quant":              report.meshpilot.quant,
            "p50_latency_ms":     report.meshpilot.p50_latency,
            "p90_latency_ms":     report.meshpilot.p90_latency,
            "avg_throughput_tps": report.meshpilot.avg_throughput,
            "avg_memory_mb":      report.meshpilot.avg_memory_mb,
            "raw_latencies":      report.meshpilot.latency_ms,
        } if report.meshpilot else None,
    }

    with open(json_path, "w") as f:
        json.dump(report_dict, f, indent=2)
    print(f"  JSON report saved: {json_path}")

    md_path = os.path.join(output_dir, "benchmark_report.md")
    with open(md_path, "w") as f:
        f.write(f"# MeshPilot Benchmark Report\n\n")
        f.write(f"**Generated:** {report.timestamp}  \n")
        f.write(f"**Platform:** {report.platform}  \n")
        f.write(f"**CPU:** {report.cpu_model} ({report.cpu_cores} cores)  \n")
        f.write(f"**RAM:** {report.ram_gb} GB  \n")
        f.write(f"**Iterations:** {report.iterations}  \n\n")
        f.write(f"All numbers below come from a live MeshPilot instance actually running "
                f"both sides of the comparison — nothing here is simulated.\n\n")
        f.write(f"## Prompt\n\n```\n{report.prompt}\n```\n\n")
        f.write(f"## Results Summary\n\n")
        f.write(f"| Metric | PyTorch FP32 | MeshPilot INT4 | Improvement |\n")
        f.write(f"|--------|-------------|----------------|-------------|\n")

        if report.pytorch_cpu and report.meshpilot:
            p, m = report.pytorch_cpu, report.meshpilot
            f.write(f"| P50 Latency | {p.p50_latency:.1f}ms | {m.p50_latency:.1f}ms | **{report.speedup_p50:.1f}x faster** |\n")
            f.write(f"| P90 Latency | {p.p90_latency:.1f}ms | {m.p90_latency:.1f}ms | **{report.speedup_p90:.1f}x faster** |\n")
            f.write(f"| Avg Throughput | {p.avg_throughput:.1f} t/s | {m.avg_throughput:.1f} t/s | **+{report.throughput_gain_pct:.0f}%** |\n")
            if report.memory_saving_pct is not None:
                f.write(f"| Memory Usage (live) | {p.avg_memory_mb:.0f} MB | {m.avg_memory_mb:.0f} MB | **-{report.memory_saving_pct:.0f}%** |\n")
            f.write(f"| Quantization | FP32 | INT4 Q4_K_M | 4-bit precision |\n\n")
            f.write(f"## Key Findings\n\n")
            f.write(f"- MeshPilot delivered **{report.speedup_p50:.1f}x faster** median inference on identical CPU hardware\n")
            if report.memory_saving_pct is not None:
                f.write(f"- Memory footprint reduced by **{report.memory_saving_pct:.0f}%** (from {p.avg_memory_mb:.0f}MB to {m.avg_memory_mb:.0f}MB), sampled live via Docker stats during inference\n")
            f.write(f"- Throughput increased by **{report.throughput_gain_pct:.0f}%** — more requests per second on the same server\n")
            f.write(f"- No GPU required — runs on any x86-64 server with AVX2 support\n\n")
            f.write(f"## Methodology\n\n")
            f.write(f"Both backends ran the *same model* on *the same machine* with identical prompts and "
                    f"token budgets ({m.completion_tokens} tokens), via MeshPilot's live `/inference/compare` "
                    f"endpoint, which runs both sides for real on every call — no offline/simulated mode exists "
                    f"in this script.\n")
            f.write(f"PyTorch baseline uses FP32 precision via plain `transformers`, no quantization, no optimization.\n")
            f.write(f"MeshPilot uses llama.cpp with Q4_K_M (INT4) quantization.\n")
            f.write(f"Most of the raw speed advantage comes from INT4 quantization + llama.cpp — an existing "
                    f"open-source inference engine MeshPilot integrates, not something unique to MeshPilot's own code.\n")

    print(f"  Markdown report saved: {md_path}")
    return json_path, md_path


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="MeshPilot Benchmark (live only, no simulation)")
    parser.add_argument("--meshpilot-url", default="http://localhost:8100",
                         help="Base URL of a running MeshPilot instance")
    parser.add_argument("--api-key", required=True,
                         help="MeshPilot API key (create one from the dashboard's API Keys page)")
    parser.add_argument("--model-id", default="benchmark-qwen25-05b",
                         help="Model to benchmark — must have a live Compare baseline deployed")
    parser.add_argument("--iterations", type=int, default=10)
    parser.add_argument("--max-tokens", type=int, default=256)
    parser.add_argument("--output-dir", default="./benchmark_output")
    args = parser.parse_args()

    PROMPT = (
        "You are a financial analyst. Explain in detail the key risks of a SaaS business "
        "targeting enterprise clients in Southeast Asia, covering market entry barriers, "
        "regulatory compliance, currency risk, and competitive dynamics. "
        "Provide specific, actionable recommendations for each risk category."
    )

    print("=" * 60)
    print("  MESHPILOT BENCHMARK SUITE (live inference only)")
    print("=" * 60)

    sysinfo = get_system_info()
    print(f"\n  Platform:   {sysinfo['platform']}")
    print(f"  CPU:        {sysinfo['cpu_model']}")
    print(f"  Cores:      {sysinfo['cpu_cores']}")
    print(f"  RAM:        {sysinfo['ram_gb']} GB")
    print(f"  Server:     {args.meshpilot_url}")
    print(f"  Model:      {args.model_id}")
    print(f"  Iterations: {args.iterations}")
    print(f"  Max tokens: {args.max_tokens}")

    try:
        pytorch_result, meshpilot_result = run_compare_benchmark(
            args.meshpilot_url, args.api_key, args.model_id,
            PROMPT, args.max_tokens, args.iterations,
        )
    except SystemExit:
        raise
    except Exception as e:
        print(f"\nERROR: could not reach MeshPilot at {args.meshpilot_url}: {e}", file=sys.stderr)
        print("This script requires a live, reachable MeshPilot instance — there is no "
              "offline/simulated fallback.", file=sys.stderr)
        sys.exit(1)

    speedup_p50, speedup_p90, mem_saving, tps_gain = print_comparison_table(pytorch_result, meshpilot_result)

    report = BenchmarkReport(
        timestamp=datetime.utcnow().isoformat() + "Z",
        platform=sysinfo["platform"],
        cpu_model=sysinfo["cpu_model"],
        cpu_cores=sysinfo["cpu_cores"],
        ram_gb=sysinfo["ram_gb"],
        iterations=args.iterations,
        prompt=PROMPT,
        pytorch_cpu=pytorch_result,
        meshpilot=meshpilot_result,
        speedup_p50=round(speedup_p50, 2),
        speedup_p90=round(speedup_p90, 2),
        memory_saving_pct=round(mem_saving, 1) if mem_saving is not None else None,
        throughput_gain_pct=round(tps_gain, 1),
    )

    print("\n  Saving reports...")
    save_report(report, args.output_dir)
    print(f"\n  Benchmark complete — every number above was measured live, not simulated.")
    print(f"  Results: {args.output_dir}/")


if __name__ == "__main__":
    main()
