precision highp float;varying vec2 vUv;
      uniform float uT,uScroll;uniform vec2 uRes,uLight,uCam;
      float h(vec3 p){p=fract(p*.3183099+.1);p*=17.;return fract(p.x*p.y*p.z*(p.x+p.y+p.z));}
      float n3(vec3 x){vec3 i=floor(x),f=fract(x);f=f*f*(3.-2.*f);
        return mix(mix(mix(h(i),h(i+vec3(1,0,0)),f.x),mix(h(i+vec3(0,1,0)),h(i+vec3(1,1,0)),f.x),f.y),
                   mix(mix(h(i+vec3(0,0,1)),h(i+vec3(1,0,1)),f.x),mix(h(i+vec3(0,1,1)),h(i+vec3(1,1,1)),f.x),f.y),f.z);}
      float fbm(vec3 p){float v=0.,a=.55;for(int i=0;i<OCT;i++){v+=a*n3(p);p=p*2.03+vec3(1.7,9.2,3.1);a*=.48;}return v;}
      float dens(vec3 p){
        vec3 q=p*.62+vec3(uT*.04,-uT*.025,uT*.06);
        float d=fbm(q+1.1*fbm(q*.6+uT*.02));           // warped billows
        d=smoothstep(.52,.8,d);                         // crisp cloud edges, clear black air between
        return d*smoothstep(-3.2,.5,p.y)*smoothstep(4.5,1.2,p.y); // thins out high and low
      }
      void main(){
        vec2 uv=(vUv-.5)*vec2(uRes.x/uRes.y,1.);
        vec3 ro=vec3(uCam.x*.6,uCam.y*.4-uScroll*1.4,-4.);
        vec3 rd=normalize(vec3(uv,1.25));
        vec3 L=vec3((uLight.x-.5)*uRes.x/uRes.y*7.,uLight.y*6.-uScroll*1.4,5.);   // the red light, behind the fog
        float dither=h(vec3(gl_FragCoord.xy,uT));         // breaks up banding between steps
        float stepLen=.42,t=.5+dither*stepLen,T=1.;vec3 col=vec3(0.);
        for(int i=0;i<STEPS;i++){
          vec3 p=ro+rd*t;
          float d=dens(p);
          if(d>.01){
            vec3 toL=L-p;float dl=length(toL);toL/=dl;
            float shadow=exp(-(dens(p+toL*.5)+dens(p+toL*1.2))*1.9); // smoke between this point and the light
            float near=1./(1.+dl*dl*.11);                   // light falls off with distance
            vec3 lit=vec3(1.,.14,.08)*near*shadow*4.2+vec3(.018,.003,.004);
            float a=1.-exp(-d*stepLen*2.6);
            col+=T*a*lit;T*=1.-a;
            if(T<.03)break;
          }
          t+=stepLen;
        }
        // The light itself glowing through whatever smoke is in front of it.
        float glow=pow(max(dot(rd,normalize(L-ro)),0.),180.)*1.6+pow(max(dot(rd,normalize(L-ro)),0.),12.)*.18;
        col+=vec3(1.,.25,.14)*glow*T;
        col=col/(1.+col);                                  // soft tone curve, no harsh clipping
        col=pow(col,vec3(.92));
        gl_FragColor=vec4(col,1.);
      }