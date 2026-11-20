// Dual-fisheye (P010) -> stabilized equirectangular (P010), one pass.
//
// One invocation = one output chroma sample = a 2x2 block of luma pixels.
// Output buffers hold packed u16 pairs (P010: 10-bit value in the high bits).
// Math mirrors osv360/cpu_ref.py + osv360/lens.py exactly.

struct LensU {
    m0: vec4<f32>,          // rows of lens_from_view (3x3), w unused
    m1: vec4<f32>,
    m2: vec4<f32>,
    fxy_cxy: vec4<f32>,     // fx, fy, cx, cy   (luma pixels)
    k: vec4<f32>,           // k1..k4
    omega_ro: vec4<f32>,    // angular velocity in lens frame (rad/s) xyz, readout time (s) w
    gain: vec4<f32>,        // Y gain, U gain, V gain, Y black level (normalized)
    misc: vec4<f32>,        // max_theta (rad), image size, chroma offset u, chroma offset v
};

struct Params {
    lens: array<LensU, 2>,
    out_size: vec4<f32>,    // W, H, blend width (rad), unused
};

@group(0) @binding(0) var<uniform> P: Params;
@group(0) @binding(1) var samp: sampler;
@group(0) @binding(2) var y0: texture_2d<f32>;
@group(0) @binding(3) var c0: texture_2d<f32>;
@group(0) @binding(4) var y1: texture_2d<f32>;
@group(0) @binding(5) var c1: texture_2d<f32>;
@group(0) @binding(6) var<storage, read_write> out_y: array<u32>;
@group(0) @binding(7) var<storage, read_write> out_c: array<u32>;

const PI: f32 = 3.14159265358979;

fn view_ray(px: vec2<f32>) -> vec3<f32> {
    // px: continuous luma pixel position (pixel centres at integers)
    let W = P.out_size.x;
    let H = P.out_size.y;
    let lon = ((px.x + 0.5) / W * 2.0 - 1.0) * PI;
    let lat = (0.5 - (px.y + 0.5) / H) * PI;
    let cl = cos(lat);
    return vec3<f32>(cl * sin(lon), -sin(lat), cl * cos(lon));
}

fn radial(L: LensU, th: f32) -> f32 {
    let t2 = th * th;
    return th * (1.0 + t2 * (L.k.x + t2 * (L.k.y + t2 * (L.k.z + t2 * L.k.w))));
}

// returns (u, v, theta) in luma pixels of the lens image
fn project(L: LensU, d: vec3<f32>) -> vec3<f32> {
    let dl = vec3<f32>(dot(L.m0.xyz, d), dot(L.m1.xyz, d), dot(L.m2.xyz, d));
    let rxy = length(dl.xy);
    let th = atan2(rxy, dl.z);
    let r = radial(L, th);
    var s = 0.0;
    if (rxy > 1e-9) { s = r / rxy; }
    var uv = L.fxy_cxy.zw + L.fxy_cxy.xy * s * dl.xy;
    // rolling shutter: rows are exposed sequentially. Re-project the ray rotated by the
    // camera rotation accumulated between mid-frame and this row's exposure time.
    let ro = L.omega_ro.w;
    if (ro > 0.0) {
        let dt = (uv.y / L.misc.y - 0.5) * ro;
        let dl2 = dl - dt * cross(L.omega_ro.xyz, dl);
        let rxy2 = length(dl2.xy);
        let th2 = atan2(rxy2, dl2.z);
        var s2 = 0.0;
        if (rxy2 > 1e-9) { s2 = radial(L, th2) / rxy2; }
        uv = L.fxy_cxy.zw + L.fxy_cxy.xy * s2 * dl2.xy;
        return vec3<f32>(uv, th2);
    }
    return vec3<f32>(uv, th);
}

fn weight0(th0: f32, th1: f32) -> f32 {
    let b = max(P.out_size.z, 1e-4);
    var w = clamp((th1 - th0) / b + 0.5, 0.0, 1.0);
    w = w * w * (3.0 - 2.0 * w);
    if (th0 > P.lens[0].misc.x) { w = 0.0; }
    if (th1 > P.lens[1].misc.x) { w = 1.0; }
    return w;
}

fn luma(i: u32, uv: vec2<f32>) -> f32 {
    let L = P.lens[i];
    let tc = (uv + 0.5) / L.misc.y;
    var v: f32;
    if (i == 0u) { v = textureSampleLevel(y0, samp, tc, 0.0).r; }
    else { v = textureSampleLevel(y1, samp, tc, 0.0).r; }
    return (v - L.gain.w) * L.gain.x + L.gain.w;
}

fn chroma(i: u32, uv: vec2<f32>) -> vec2<f32> {
    let L = P.lens[i];
    let cs = L.misc.y * 0.5;
    // 4:2:0 left-cosited horizontally, centred vertically
    let tc = vec2<f32>((uv.x * 0.5 + 0.5) / cs, ((uv.y - 0.5) * 0.5 + 0.5) / cs);
    var c: vec2<f32>;
    if (i == 0u) { c = textureSampleLevel(c0, samp, tc, 0.0).rg; }
    else { c = textureSampleLevel(c1, samp, tc, 0.0).rg; }
    return (c - vec2<f32>(0.5)) * L.gain.yz + vec2<f32>(0.5) + L.misc.zw;
}

// unorm16 holds code<<6, so code = v * 65535 / 64
fn pack10(a: f32, b: f32) -> u32 {
    let ia = min(u32(clamp(a, 0.0, 1.0) * 1023.984375 + 0.5), 1023u) << 6u;
    let ib = min(u32(clamp(b, 0.0, 1.0) * 1023.984375 + 0.5), 1023u) << 6u;
    return ia | (ib << 16u);
}

fn sample_luma(px: vec2<f32>) -> f32 {
    let d = view_ray(px);
    let p0 = project(P.lens[0], d);
    let p1 = project(P.lens[1], d);
    let w = weight0(p0.z, p1.z);
    var v = 0.0;
    if (w > 0.0) { v += w * luma(0u, p0.xy); }
    if (w < 1.0) { v += (1.0 - w) * luma(1u, p1.xy); }
    return v;
}

@compute @workgroup_size(16, 8, 1)
fn main(@builtin(global_invocation_id) gid: vec3<u32>) {
    let cw = u32(P.out_size.x) / 2u;
    let ch = u32(P.out_size.y) / 2u;
    if (gid.x >= cw || gid.y >= ch) { return; }
    let x = f32(gid.x * 2u);
    let y = f32(gid.y * 2u);

    let a = sample_luma(vec2<f32>(x, y));
    let b = sample_luma(vec2<f32>(x + 1.0, y));
    let c = sample_luma(vec2<f32>(x, y + 1.0));
    let e = sample_luma(vec2<f32>(x + 1.0, y + 1.0));
    let W = u32(P.out_size.x);
    out_y[(gid.y * 2u) * (W / 2u) + gid.x] = pack10(a, b);
    out_y[(gid.y * 2u + 1u) * (W / 2u) + gid.x] = pack10(c, e);

    // chroma sample position: left-cosited, vertically between the two luma rows
    let d = view_ray(vec2<f32>(x, y + 0.5));
    let p0 = project(P.lens[0], d);
    let p1 = project(P.lens[1], d);
    let w = weight0(p0.z, p1.z);
    var uv = vec2<f32>(0.0);
    if (w > 0.0) { uv += w * chroma(0u, p0.xy); }
    if (w < 1.0) { uv += (1.0 - w) * chroma(1u, p1.xy); }
    out_c[gid.y * cw + gid.x] = pack10(uv.x, uv.y);
}
