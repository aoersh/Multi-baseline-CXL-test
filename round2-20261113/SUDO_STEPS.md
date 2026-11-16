# 需要用户执行的 sudo 步骤

本轮实验的绝大部分在普通用户下完成，不需要 sudo。只有下面两项需要 root。

## 1. 预取器判别实验（关键，约 40 秒 × 2）

背景：MLC 报告 12% 的样本落在 0–31 个基频周期内（< 14 ns），这在 256 MiB
随机指针追逐里物理上不可能。我写的独立逐次计时器只测到 0.003%–0.05%。
现在需要确定这个伪影的来源：是硬件预取器，还是 MLC 自身采样流程。

脚本做的事：读取 MSR 0x1A4（L2 / L2-adjacent / DCU / DCU-IP 四个预取器位），
在"全部关闭"和"保持原值"两种状态下各跑一次同样的测量，**退出时由 trap
无条件恢复原值**。不改 BIOS、不 retrain 链路、不碰 AER、不 reboot。

先做只读盘点（可选）：

```bash
sudo bash /home/hjy/tools/experiments/cxl-tail-20261109/round2-20261113/prefetch_toggle.sh snap
```

然后跑两次对照：

```bash
cd /home/hjy/tools/experiments/cxl-tail-20261109/round2-20261113
sudo bash prefetch_toggle.sh off  0 2 n2-noPrefetch
sudo bash prefetch_toggle.sh orig 0 2 n2-PrefetchOn
```

判读方式：

- 若 `off` 时两个工具的 sub-32 比例**同时塌掉** → 伪影来自硬件预取器。
  那么第 1 轮报告里所有绝对值都偏低，需要重算。
- 若 `off` 时 MLC 仍是 ~12% 而 chase 仍接近 0 → 伪影来自 MLC 采样流程，
  与硬件无关，报告里的 MLC 直方图整体不可信，应以逐次计时器替代。
- 若 `off` 报错（MSR 被 BIOS 锁定）→ 说明该平台不允许软件关闭预取器，
  改用步长/访问模式判别（已在本轮矩阵里并行做）。

脚本依赖同目录下已编译好的 `msr_rw`。若缺失：

```bash
gcc -O2 -Wall -o msr_rw msr_rw.c
```

## 2. 链路状态前后差（可选，第 1 轮遗留的 P1）

第 1 轮报告第 7 节那条，用于给节点间链路错误一个同次实验的前后增量：

```bash
sudo bash /home/hjy/tools/experiments/cxl-tail-20261109/capture_link_state.sh before
numactl --physcpubind=0 --membind=2 /home/hjy/tools/memtraffic 512 10
sudo bash /home/hjy/tools/experiments/cxl-tail-20261109/capture_link_state.sh after
```

这项与本轮的预取器判别互不影响，可以在矩阵跑完之后再做。
