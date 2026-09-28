// CC0: Abstract black hole
// Experimenting with a black hole effect, light rays bend toward the center mass.
// Abstract coloring via layered cylinders in a disk.

// Multi-pass shader: main effect is in Buffer A.

// This file is released under CC0 1.0 Universal (Public Domain Dedication).
// To the extent possible under law, mrange has waived all copyright
// and related or neighboring rights to this work.
// See <https://creativecommons.org/publicdomain/zero/1.0/> for details.

const vec3
  ro=vec3(0,1,8)
, la=vec3(0,0,0)
, ZZ=normalize(la-ro)
, XX=normalize(cross(ZZ,vec3(-.2,1,0)))
, YY=cross(XX,ZZ)
;

const float
  PI    =acos(-1.)
, TAU   =2.*PI
, PI_2  =.5*PI
, STEP  =.05
, REPS  =2.
;

// License: MIT, author: Pascal Gilcher, found: https://www.shadertoy.com/view/flSXRV
float atan_approx(float y, float x) {
  float
    cosatan2  = x/(abs(x)+abs(y))
  , t         = -cosatan2*PI_2+PI_2
  ;
  return y<0.?-t:t;
}

// License: Unknown, author: Martin Roberts, found: https://extremelearning.com.au/unreasonable-effectiveness-of-quasirandom-sequences/
vec2 jitter(int frame) {
  return fract(float(frame)*vec2(.7548776662, .5698402910))-.5;
}

// License: Unknown, author: Claude Brezinski, found: https://mathr.co.uk/blog/2017-09-06_approximating_hyperbolic_tangent.html
vec3 tanh_approx(vec3 x) {
  //  Found this somewhere on the interwebs
  //  return tanh(x);
  vec3 x2 = x*x;
  return clamp(x*(27.0 + x2)/(27.0+9.0*x2), -1.0, 1.0);
}

float uhash(vec2 co) {
  uvec2 k = floatBitsToUint(co);
  uint h = k.x ^ (k.y * 0x9e3779b9u);
  h ^= h >> 16u;
  h *= 0x85ebca6bu;
  h ^= h >> 13u;
  h *= 0xc2b2ae35u;
  h ^= h >> 16u;
  return float(h) / 4294967295.0;
}

float height(float p) {
  p-=123.4;
  p*=3.;
  float
    A=1.
  , h=0.
  ;
  for(int i=0;i<3;++i) {
    h+=A*(.5+.5*sin(p));
    A*=.5;
    p=2.03*p+1.23;

  }
  return .25*h;
}

void mainImage(out vec4 FC, vec2 C) {
  C+=jitter(iFrame);
  vec2
    R=iResolution.xy
  , P=(C+C-R)/R.y
  , w
  ;
  vec3
    rd=normalize(P.y*YY-P.x*XX+2.*ZZ)
  , crd=rd
  , p
  , o
  , g
  ;
  float j,z=0.,d,D,L,l,s,N,a,H,A,Z;
  vec4 O;

  for (int i=0; i<99&&z<29.;++i) {
    p=z*crd+ro;
    L=dot(p,p);
    l=sqrt(L);
    d=l-.7;
    if(d<1e-2) break;
    Z=length(p.xz);
    g=-p/(l*L);
    a=atan_approx(p.z,p.x);
    N=clamp(floor(length(p.xz)/STEP+.5),1./STEP,6./STEP);
    for(j=-REPS;j<=REPS;++j) {
      H=uhash(vec2(N,j));
      A=a+H*TAU+iTime/(2.*N*STEP);
      w=vec2(abs(Z-STEP*(N+j/(2.*REPS+1.))), abs(p.y)-height(A)/(N*STEP));
      D=min(max(w.x,w.y),0.)+length(max(w,0.));
      d=min(d,D);
      O=(160.*(1.-dot(crd,rd))+1.+sin(a-1.*(N*STEP)+2.5*H+vec4(7,2,9,7)))/(1.+N*STEP);
      o+=O.w/max(D,1e-3)*O.xyz;
    }
    d=abs(d)+1e-2;

    s=.5*d;
    crd=normalize(crd+g*.15*s);
    z+=s;
  }

  o/=5e3;
  o=tanh_approx(o);
  o*=smoothstep(.0,.2,dot(o,vec3(.299, .587, .114)));
  o=mix(o,texelFetch(iChannel0, ivec2(C), 0).xyz,.9);
  FC=vec4(o,1);
}
