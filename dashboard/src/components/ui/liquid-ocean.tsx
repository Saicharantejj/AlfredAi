"use client";

import React, { useRef, useMemo } from "react";
import { Canvas, useFrame } from "@react-three/fiber";
import * as THREE from "three";
import { Float, Environment } from "@react-three/drei";
import { cn } from "@/lib/utils";

interface LiquidOceanProps {
  backgroundColor?: number;
  accentColor?: number;
  boatCount?: number;
  children?: React.ReactNode;
  className?: string;
}

const OceanShader = {
  uniforms: {
    uTime: { value: 0 },
    uColor: { value: new THREE.Color(0x00ffff) },
    uBackgroundColor: { value: new THREE.Color(0x0a1a1a) },
  },
  vertexShader: `
    varying vec2 vUv;
    varying float vElevation;
    uniform float uTime;

    void main() {
      vUv = uv;
      vec4 modelPosition = modelMatrix * vec4(position, 1.0);
      
      float elevation = sin(modelPosition.x * 2.0 + uTime) * 
                        cos(modelPosition.z * 1.5 + uTime * 0.8) * 0.2;
      
      modelPosition.y += elevation;
      vElevation = elevation;

      vec4 viewPosition = viewMatrix * modelPosition;
      vec4 projectionPosition = projectionMatrix * viewPosition;
      gl_Position = projectionPosition;
    }
  `,
  fragmentShader: `
    varying vec2 vUv;
    varying float vElevation;
    uniform vec3 uColor;
    uniform vec3 uBackgroundColor;

    void main() {
      float mixStrength = (vElevation + 0.2) * 2.0;
      vec3 color = mix(uBackgroundColor, uColor, clamp(mixStrength, 0.0, 1.0));
      gl_FragColor = vec4(color, 1.0);
    }
  `,
};

function OceanSurface({ accentColor, backgroundColor }: { accentColor: number; backgroundColor: number }) {
  const meshRef = useRef<THREE.Mesh>(null);
  const materialRef = useRef<THREE.ShaderMaterial>(null);

  const uniforms = useMemo(
    () => ({
      uTime: { value: 0 },
      uColor: { value: new THREE.Color(accentColor) },
      uBackgroundColor: { value: new THREE.Color(backgroundColor) },
    }),
    [accentColor, backgroundColor]
  );

  useFrame((state) => {
    if (materialRef.current) {
      materialRef.current.uniforms.uTime.value = state.clock.getElapsedTime();
    }
  });

  return (
    <mesh ref={meshRef} rotation={[-Math.PI / 2, 0, 0]} position={[0, -0.5, 0]}>
      <planeGeometry args={[20, 20, 128, 128]} />
      <shaderMaterial
        ref={materialRef}
        vertexShader={OceanShader.vertexShader}
        fragmentShader={OceanShader.fragmentShader}
        uniforms={uniforms}
        wireframe={false}
      />
    </mesh>
  );
}

function Boat({ color, index }: { color: number; index: number }) {
  const meshRef = useRef<THREE.Mesh>(null);
  const speed = 0.5 + Math.random();
  const offset = index * 2;

  useFrame((state) => {
    if (meshRef.current) {
      const time = state.clock.getElapsedTime();
      meshRef.current.position.y = Math.sin(time * speed + offset) * 0.1;
      meshRef.current.rotation.x = Math.sin(time * 0.5 + offset) * 0.1;
      meshRef.current.rotation.z = Math.cos(time * 0.3 + offset) * 0.1;
    }
  });

  const position: [number, number, number] = [
    (Math.random() - 0.5) * 6,
    0,
    (Math.random() - 0.5) * 6,
  ];

  return (
    <Float speed={2} rotationIntensity={0.5} floatIntensity={0.5}>
      <mesh ref={meshRef} position={position}>
        <boxGeometry args={[0.4, 0.4, 0.4]} />
        <meshStandardMaterial color={color} roughness={0.1} metalness={0.8} />
      </mesh>
    </Float>
  );
}

export function LiquidOcean({
  backgroundColor = 0x0a1a1a,
  accentColor = 0x00ffff,
  boatCount = 5,
  children,
  className,
}: LiquidOceanProps) {
  return (
    <div className={cn("relative w-full h-full overflow-hidden", className)}>
      <Canvas camera={{ position: [0, 2, 5], fov: 45 }}>
        <color attach="background" args={[new THREE.Color(backgroundColor)]} />
        <ambientLight intensity={0.5} />
        <pointLight position={[10, 10, 10]} intensity={1.5} color={accentColor} />
        <spotLight
          position={[-10, 10, 10]}
          angle={0.15}
          penumbra={1}
          intensity={1}
          castShadow
        />
        
        <OceanSurface accentColor={accentColor} backgroundColor={backgroundColor} />
        
        {Array.from({ length: boatCount }).map((_, i) => (
          <Boat key={i} index={i} color={accentColor} />
        ))}
        
        <Environment preset="city" />
      </Canvas>
      
      {children && (
        <div className="absolute inset-0 flex items-center justify-center z-10 pointer-events-none">
          {children}
        </div>
      )}
    </div>
  );
}
