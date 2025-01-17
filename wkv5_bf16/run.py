import os
# os.environ["CUDA_VISIBLE_DEVICES"] = "3"
import torch
from torch.utils.cpp_extension import load
from torch.nn import functional as F
import numpy as np
from math import exp
import sys

np.set_printoptions(precision=4, suppress=True, linewidth=200)
# turn off TF32 for higher accuracy
torch.backends.cudnn.allow_tf32 = False
torch.backends.cuda.matmul.allow_tf32 = False

DTYPE = torch.bfloat16
DEVICE = 'cuda'
CUDA_KERNEL_VERSION = 'v1b2'

JOB = sys.argv[1].strip()

# ORIGINAL
B = 8
T = 4096
C = 4096
HEAD_SIZE = 128
H = C // HEAD_SIZE
CHUNK_LEN = 512

# DEBUG
# B = 2
# T = 4
# C = 8
# HEAD_SIZE = 4
# H = C // HEAD_SIZE
# CHUNK_LEN = 2


def set_seed(seed):
    np.random.seed(seed)
    torch.manual_seed(seed)
    torch.cuda.manual_seed_all(seed)

def get_err_ratio(x, y):
    err = (x-y).flatten().square().mean().sqrt().item()
    base = (x).flatten().square().mean().sqrt().item()
    return err / base

########################################################################################################
# CUDA Kernel
########################################################################################################

wkv5_cuda = load(name="wkv5", sources=["cuda/wkv5_op.cpp", f"cuda/wkv5_cuda_{CUDA_KERNEL_VERSION}.cu"],
                verbose=True, extra_cuda_cflags=["-res-usage", "--use_fast_math", "-O3", "-Xptxas -O3", "--extra-device-vectorization", f"-D_N_={HEAD_SIZE}"])
    
class WKV_5(torch.autograd.Function):
    @staticmethod
    def forward(ctx, B, T, C, H, r, k, v, w, u):
        with torch.no_grad():
            assert r.dtype == torch.bfloat16
            assert k.dtype == torch.bfloat16
            assert v.dtype == torch.bfloat16
            assert w.dtype == torch.bfloat16
            assert u.dtype == torch.bfloat16
            assert HEAD_SIZE == C // H
            ctx.B = B
            ctx.T = T
            ctx.C = C
            ctx.H = H
            assert r.is_contiguous()
            assert k.is_contiguous()
            assert v.is_contiguous()
            assert w.is_contiguous()
            assert u.is_contiguous()
            ew = (-torch.exp(w.float())).contiguous()
            eew = (torch.exp(ew)).contiguous()
            ctx.save_for_backward(r, k, v, eew, ew, u)
            y = torch.empty((B, T, C), device=r.device, dtype=torch.bfloat16, memory_format=torch.contiguous_format) # .uniform_(-1, 1)
            wkv5_cuda.forward(B, T, C, H, r, k, v, eew, u, y)
            return y

    @staticmethod
    def backward(ctx, gy):
        with torch.no_grad():
            assert gy.dtype == torch.bfloat16
            B = ctx.B
            T = ctx.T
            C = ctx.C
            H = ctx.H
            assert gy.is_contiguous()
            r, k, v, eew, ew, u = ctx.saved_tensors
            gr = torch.empty((B, T, C), device=gy.device, requires_grad=False, dtype=torch.bfloat16, memory_format=torch.contiguous_format) # .uniform_(-1, 1)
            gk = torch.empty((B, T, C), device=gy.device, requires_grad=False, dtype=torch.bfloat16, memory_format=torch.contiguous_format) # .uniform_(-1, 1)
            gv = torch.empty((B, T, C), device=gy.device, requires_grad=False, dtype=torch.bfloat16, memory_format=torch.contiguous_format) # .uniform_(-1, 1)
            gw = torch.empty((B, C), device=gy.device, requires_grad=False, dtype=torch.bfloat16, memory_format=torch.contiguous_format) # .uniform_(-1, 1)
            gu = torch.empty((B, C), device=gy.device, requires_grad=False, dtype=torch.bfloat16, memory_format=torch.contiguous_format) # .uniform_(-1, 1)
            wkv5_cuda.backward(B, T, C, H, r, k, v, eew, ew, u, gy, gr, gk, gv, gw, gu)
            gw = torch.sum(gw, 0).view(H, C//H)
            gu = torch.sum(gu, 0).view(H, C//H)
            return (None, None, None, None, gr, gk, gv, gw, gu)

def RUN_CUDA(B, T, C, H, r, k, v, w, u):
    return WKV_5.apply(B, T, C, H, r, k, v, w, u)

######################################################################################################
# Original python version
######################################################################################################

def RUN_FORMULA_1(B, T, C, H, r, k, v, w, u):
    N = C // H
    r = r.view(B, T, H, N)
    k = k.view(B, T, H, N)
    v = v.view(B, T, H, N)
    w = w.view(H, N)
    u = u.view(H, N)
    out = torch.zeros((B, T, H, N), device=DEVICE)

    for b in range(B):
        for h in range(H):
            for t in range(T):
                for i in range(N):
                    for j in range(N):
                        for tt in range(t+1):
                            ww = u[h,j] if (tt == t) else w[h,j] ** (t - tt - 1)
                            out[b,t,h,i] += r[b,t,h,j] * ww * k[b,tt,h,j] * v[b,tt,h,i]

    return out.view(B, T, C)

def RUN_BACKWARD_1(B, T, C, H, gy, r, k, v, __w, u):
    N = C // H
    gy = gy.view(B, T, H, N)
    r = r.view(B, T, H, N)
    k = k.view(B, T, H, N)
    v = v.view(B, T, H, N)
    _w = -torch.exp(__w).view(H, N)
    u = u.view(H, N)
    w = torch.exp(_w)

    gr = torch.zeros((B, T, H, N), device=DEVICE)
    gk = torch.zeros((B, T, H, N), device=DEVICE)
    gv = torch.zeros((B, T, H, N), device=DEVICE)
    gw = torch.zeros((H, N), device=DEVICE)
    gu = torch.zeros((H, N), device=DEVICE)

    for b in range(B):
        for h in range(H):
            for i in range(N):
                for t in range(T):
                    for j in range(N):

                        for tt in range(t+1):
                            ww = u[h,i] if (tt == t) else w[h,i] ** (t - tt - 1)
                            gr[b,t,h,i] += ww * k[b,tt,h,i] * v[b,tt,h,j] * gy[b,t,h,j]

                        for tt in range(t,T):
                            ww = u[h,i] if (tt == t) else w[h,i] ** (tt - t - 1)
                            gk[b,t,h,i] += r[b,tt,h,i] * ww * v[b,t,h,j] * gy[b,tt,h,j]

                            ww = u[h,j] if (tt == t) else w[h,j] ** (tt - t - 1)
                            gv[b,t,h,i] += r[b,tt,h,j] * ww * k[b,t,h,j] * gy[b,tt,h,i]

                        gu[h,i] += r[b,t,h,i] * k[b,t,h,i] * v[b,t,h,j] * gy[b,t,h,j]

                        for tt in range(t-1):
                            ww = (t-tt-1) * _w[h,i] * (w[h,i] ** (t - tt - 1))
                            gw[h,i] += r[b,t,h,i] * ww * k[b,tt,h,i] * v[b,tt,h,j] * gy[b,t,h,j]

    return gr.view(B, T, C), gk.view(B, T, C), gv.view(B, T, C), gw.view(C), gu.view(C)

######################################################################################################
# Original pytorch version (requires w & u to be constant within each head)
######################################################################################################

class RUN_TORCH(torch.jit.ScriptModule):
    def __init__(self, chunk_len):
        super().__init__()
        self.chunk_len = chunk_len

    @torch.jit.script_method
    def jit_func(self, r, k, v, w, wk, wb, ws):
        B, T, C = r.size()
        H = w.size()[1]
        Z = self.chunk_len
        N = C // H
        r = r.view(B, T, H, N).transpose(1, 2) # BTC -> BHTN
        k = k.view(B, T, H, N).transpose(1, 2).transpose(-2, -1) # BTC -> BHTN -> BHNT
        v = v.view(B, T, H, N).transpose(1, 2) # BTC -> BHTN

        s = torch.zeros(B, H, N, N, device=r.device, dtype=r.dtype) # state
        x = torch.zeros(B, H, T, N, device=r.device, dtype=r.dtype) # output

        for i in range(T // Z):
            rr = r[:, :, i*Z:i*Z+Z, :]
            kk = k[:, :, :, i*Z:i*Z+Z]
            vv = v[:, :, i*Z:i*Z+Z, :]
            x[:, :, i*Z:i*Z+Z, :] = ((rr @ kk) * w) @ vv  +  (rr @ s) * wb
            s = ws * s + (kk * wk) @ vv

        return x.transpose(1, 2).contiguous().view(B, T, C) # BHTN -> BTHN -> BTC

    def forward(self, B, T, C, H, r, k, v, w, u):
        w = w.view(H, 1)
        u = u.view(H, 1)
        Z = self.chunk_len

        ws = w.pow(Z).reshape(1, H, 1, 1)

        ind = torch.arange(Z-1, -1, -1, device=r.device).unsqueeze(0).repeat(H, 1)
        w = w.repeat(1, Z).pow(ind)

        wk = w.reshape(1, H, 1, Z)
        wb = wk.transpose(-2, -1).flip(2)

        w = torch.cat([w[:, 1:], u], dim=1)
        w = F.pad(w, (0, Z))
        w = torch.tile(w, [Z])
        w = w[:, :-Z].reshape(-1, Z, 2 * Z - 1)
        w = w[:, :, Z-1:].reshape(1, H, Z, Z)

        w = w.to(dtype=r.dtype)
        wk = wk.to(dtype=r.dtype)
        wb = wb.to(dtype=r.dtype)
        ws = ws.to(dtype=r.dtype)

        return self.jit_func(r, k, v, w, wk, wb, ws)

######################################################################################################
# Check correctness
######################################################################################################

def CHECK_CORRECTNESS_PYTHON_FLOAT32():
    def LOSS(y): # a strange loss for better verification
        return ((y * y) - torch.tanh(y)).sum()

    set_seed(42)

    # torch
    with torch.no_grad():
        r = torch.zeros(B, T, C, device=DEVICE).uniform_(-1, 1).float()
        k = torch.zeros(B, T, C, device=DEVICE).uniform_(-1, 1).float()
        v = torch.zeros(B, T, C, device=DEVICE).uniform_(-1, 1).float()
        w = torch.zeros(H, device=DEVICE).uniform_(-8, 1).float()
        u = torch.zeros(H, device=DEVICE).uniform_(-1, 1).float()
    r.requires_grad_()
    k.requires_grad_()
    v.requires_grad_()
    w.requires_grad_()
    u.requires_grad_()

    print(f'B={B} T={T} C={C} HEAD_SIZE={HEAD_SIZE}')
    print('[Torch (const w & u within a head)] vs Python naive formula')
    rwkv5_torch = RUN_TORCH(chunk_len = CHUNK_LEN)

    # collect fp32 reference values
    y = rwkv5_torch.forward(B, T, C, H, r, k, v, torch.exp(-torch.exp(w)), u)
    LOSS(y).backward()

    gr = r.grad.data.clone()
    gk = k.grad.data.clone()
    gv = v.grad.data.clone()
    gw = w.grad.data.clone()
    gu = u.grad.data.clone()

    # Naive
    with torch.no_grad():
        # the w and u for python version use torch.zeros(C) instead of torch.zeros(H)
        w_naive = w.unsqueeze(1).repeat(1, HEAD_SIZE).clone()
        u_naive = u.unsqueeze(1).repeat(1, HEAD_SIZE).clone()

    w_naive.requires_grad_()
    u_naive.requires_grad_()

    r.grad.data.zero_()
    k.grad.data.zero_()
    v.grad.data.zero_()
    w.grad.data.zero_()
    u.grad.data.zero_()

    y_naive_fp32 = RUN_FORMULA_1(B, T, C, H, r, k, v, torch.exp(-torch.exp(w_naive)), u_naive)
    yy_naive_fp32 = y_naive_fp32.clone().detach().requires_grad_(True)
    LOSS(yy_naive_fp32).backward()
    gy_naive_fp32 = yy_naive_fp32.grad.data.clone()

    gr_naive_fp32, gk_naive_fp32, gv_naive_fp32, gw_naive_fp32, gu_naive_fp32 = RUN_BACKWARD_1(B, T, C, H, gy_naive_fp32, r, k, v, w_naive, u_naive)
    
    gw_naive_fp32 = gw_naive_fp32.view(H, C//H).sum(1)
    gu_naive_fp32 = gu_naive_fp32.view(H, C//H).sum(1)

    print('!!! [Torch float32 vs PYTHON NAIVE fp32] y (err ratio) =', get_err_ratio(y, y_naive_fp32))
    print('--> [Torch float32 vs PYTHON NAIVE fp32] g_r (err ratio) =', get_err_ratio(gr, gr_naive_fp32))
    print('--> [Torch float32 vs PYTHON NAIVE fp32] g_k (err ratio) =', get_err_ratio(gk, gk_naive_fp32))
    print('--> [Torch float32 vs PYTHON NAIVE fp32] g_v (err ratio) =', get_err_ratio(gv, gv_naive_fp32))
    print('--> [Torch float32 vs PYTHON NAIVE fp32] g_w err ratio) =', get_err_ratio(gw, gw_naive_fp32))
    print('--> [Torch float32 vs PYTHON NAIVE fp32] g_u (err ratio) =', get_err_ratio(gu, gu_naive_fp32))


def CHECK_CORRECTNESS_CUDA_BF16():
    def LOSS(y): # a strange loss for better verification
        return ((y * y) - torch.tanh(y)).sum()

    set_seed(42)
    with torch.no_grad():
        r = torch.zeros(B, T, C, device=DEVICE).uniform_(-1, 1).to(dtype=DTYPE).to(dtype=DTYPE)
        k = torch.zeros(B, T, C, device=DEVICE).uniform_(-1, 1).to(dtype=DTYPE).to(dtype=DTYPE)
        v = torch.zeros(B, T, C, device=DEVICE).uniform_(-1, 1).to(dtype=DTYPE).to(dtype=DTYPE)
        w = torch.zeros(H, device=DEVICE).uniform_(-8, 1).to(dtype=DTYPE).to(dtype=DTYPE)
        u = torch.zeros(H, device=DEVICE).uniform_(-1, 1).to(dtype=DTYPE).to(dtype=DTYPE)
    
    r.requires_grad_()
    k.requires_grad_()
    v.requires_grad_()
    w.requires_grad_()
    u.requires_grad_()

    print(f'B={B} T={T} C={C} HEAD_SIZE={HEAD_SIZE}')
    print('[torch bf16 (const w & u within a head)] vs CUDA bf16')
    rwkv5_torch = RUN_TORCH(chunk_len = CHUNK_LEN)
    
    # collect bf16 reference values
    y = rwkv5_torch.forward(B, T, C, H, r, k, v, torch.exp(-torch.exp(w)), u)
    LOSS(y).backward()
    
    gr = r.grad.data.clone()
    gk = k.grad.data.clone()
    gv = v.grad.data.clone()
    gw = w.grad.data.clone()
    gu = u.grad.data.clone()

    r.grad.data.zero_()
    k.grad.data.zero_()
    v.grad.data.zero_()
    w.grad.data.zero_()
    u.grad.data.zero_()

    # collect CUDA bf16 values
    with torch.no_grad():
        ww = w.unsqueeze(1).repeat(1, HEAD_SIZE).clone().to(dtype=DTYPE)
        uu = u.unsqueeze(1).repeat(1, HEAD_SIZE).clone().to(dtype=DTYPE)
    
    ww.requires_grad_()
    uu.requires_grad_()

    y_cuda_bf16 = RUN_CUDA(B, T, C, H, r, k, v, ww, uu)
    LOSS(y_cuda_bf16).backward()

    gr_cuda_bf16 = r.grad.data.clone()
    gk_cuda_bf16 = k.grad.data.clone()
    gv_cuda_bf16 = v.grad.data.clone()
    gw_cuda_bf16 = ww.grad.data.clone().view(H, C//H).sum(1)
    gu_cuda_bf16 = uu.grad.data.clone().view(H, C//H).sum(1)

    print('!!! [Torch bf16 vs CUDA bf16] y (err ratio) =', get_err_ratio(y, y_cuda_bf16))
    print('!!! [Torch bf16 vs CUDA bf16] g_r (err ratio) =', get_err_ratio(gr, gr_cuda_bf16))
    print('!!! [Torch bf16 vs CUDA bf16] g_k (err ratio) =', get_err_ratio(gk, gk_cuda_bf16))
    print('!!! [Torch bf16 vs CUDA bf16] g_v (err ratio) =', get_err_ratio(gv, gv_cuda_bf16))
    print('!!! [Torch bf16 vs CUDA bf16] g_w (err ratio) =', get_err_ratio(gw, gw_cuda_bf16))
    print('!!! [Torch bf16 vs CUDA bf16] g_u (err ratio) =', get_err_ratio(gu, gu_cuda_bf16))


def CHECK_SPEED():
    print('__CUDNN VERSION:', torch.backends.cudnn.version())
    print('__Number CUDA Devices:', torch.cuda.device_count())
    print('__CUDA Device Name:',torch.cuda.get_device_name(0))
    print('__CUDA Device Total Memory [GB]:',torch.cuda.get_device_properties(0).total_memory/1e9)
    print("=======")
    print(f'B={B} T={T} C={C} HEAD_SIZE={HEAD_SIZE}')
    
    def LOSS(y): # a strange loss for better verification
        return ((y * y) - torch.tanh(y)).sum()

    set_seed(42)
    with torch.no_grad():
        r = torch.zeros(B, T, C, requires_grad=True, device=DEVICE).uniform_(-1, 1).to(dtype=DTYPE)
        k = torch.zeros(B, T, C, requires_grad=True, device=DEVICE).uniform_(-1, 1).to(dtype=DTYPE)
        v = torch.zeros(B, T, C, requires_grad=True, device=DEVICE).uniform_(-1, 1).to(dtype=DTYPE)
        w = torch.zeros(H, requires_grad=True, device=DEVICE).uniform_(-8, 1).to(dtype=DTYPE)
        u = torch.zeros(H, requires_grad=True, device=DEVICE).uniform_(-1, 1).to(dtype=DTYPE)
        ww = w.unsqueeze(1).repeat(1, HEAD_SIZE).clone().to(dtype=DTYPE)
        uu = u.unsqueeze(1).repeat(1, HEAD_SIZE).clone().to(dtype=DTYPE)

    r.requires_grad_()
    k.requires_grad_()
    v.requires_grad_()
    ww.requires_grad_()
    uu.requires_grad_()

    y = RUN_CUDA(B, T, C, H, r, k, v, ww, uu)

    with torch.autograd.profiler.profile(use_cuda=True) as prof:
        y = RUN_CUDA(B, T, C, H, r, k, v, ww, uu)
    
    print('CUDA forward:\n', prof.key_averages(group_by_stack_n=5).table(sort_by='self_cuda_time_total', row_limit=3))
    
    with torch.autograd.profiler.profile(use_cuda=True) as prof:
        LOSS(y).backward()
    
    print('CUDA backward:\n', prof.key_averages(group_by_stack_n=5).table(sort_by='self_cuda_time_total', row_limit=3))
    
if __name__ == "__main__":

    if JOB == 'check-python-f32':
        print('\n\nCheck Python naive (forward + backward) with torch f32 ...')
        CHECK_CORRECTNESS_PYTHON_FLOAT32()
    elif JOB == "check-cuda-bf16":
        print(f'\n\nCheck CUDA (BFLOAT16) {CUDA_KERNEL_VERSION} (forward + backward) with torch bf16...')
        CHECK_CORRECTNESS_CUDA_BF16()
    elif JOB == "benchmark":
        print(f'\n\nCheck CUDA kernel {CUDA_KERNEL_VERSION} speed...')
        CHECK_SPEED()
    else:
        print(f'Unknown JOB: {JOB}')