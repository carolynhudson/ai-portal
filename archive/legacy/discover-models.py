#!/usr/bin/env python3
"""
llama.cpp Model Inventory and Metadata Extractor
Builds enriched INDEX.json from GGUF files and HuggingFace metadata
"""

import json
import os
import struct
import re
from pathlib import Path
from dataclasses import dataclass, asdict
from typing import Optional, Dict, List
import hashlib

try:
    from huggingface_hub import model_info, HfApi
    HF_AVAILABLE = True
except ImportError:
    HF_AVAILABLE = False
    print("Warning: huggingface-hub not installed. Install with: pip install huggingface-hub")


@dataclass
class ModelMetadata:
    """Core metadata structure for indexed models"""
    filename: str
    full_path: str
    model_name: str  # Friendly name
    model_id: Optional[str] = None  # HuggingFace model ID
    
    # Basic characteristics
    parameter_count: Optional[int] = None
    quantization_type: Optional[str] = None
    quantization_bits: Optional[float] = None
    file_size_bytes: Optional[int] = None
    
    # Architecture details
    context_window: Optional[int] = None
    architecture: Optional[str] = None
    attention_type: Optional[str] = None
    uses_rope: Optional[bool] = None
    rope_freq_scale: Optional[float] = None
    
    # Capabilities
    supports_tool_use: Optional[bool] = None
    supports_reasoning: Optional[bool] = None
    is_moe: Optional[bool] = None
    moe_expert_count: Optional[int] = None
    moe_expert_used: Optional[int] = None
    supports_vision: Optional[bool] = None
    supports_audio: Optional[bool] = None
    
    # Deployment hints
    is_speculative_decoding_draft: Optional[bool] = None
    speculative_target_model: Optional[str] = None
    supports_rag: Optional[bool] = None
    recommended_backend: Optional[str] = None  # "vulkan", "rocm", "auto"
    
    # Quantization variants (for split models)
    is_split_model: bool = False
    split_count: Optional[int] = None
    split_index: Optional[int] = None
    sibling_files: List[str] = None
    
    # Metadata sources
    metadata_sources: List[str] = None  # Track where info came from
    hf_model_card_url: Optional[str] = None
    last_updated: str = None


class GGUFParser:
    """Extract metadata directly from GGUF file headers"""
    
    GGUF_MAGIC = 0x46554747  # "GGUF" in little-endian
    
    def __init__(self, filepath: str):
        self.filepath = filepath
        self.metadata = {}
    
    def parse(self) -> Optional[Dict]:
        """
        Parse GGUF file header for metadata.
        Returns dict of extracted metadata or None if parse fails.
        """
        try:
            with open(self.filepath, 'rb') as f:
                # Check magic number
                magic = struct.unpack('<I', f.read(4))[0]
                if magic != self.GGUF_MAGIC:
                    return None
                
                # Parse version (version 3)
                version = struct.unpack('<I', f.read(4))[0]
                
                # Skip to metadata section
                tensor_count = struct.unpack('<Q', f.read(8))[0]
                metadata_kv_count = struct.unpack('<Q', f.read(8))[0]
                
                metadata = {}
                for _ in range(metadata_kv_count):
                    # Read key
                    key_len = struct.unpack('<I', f.read(4))[0]
                    key = f.read(key_len).decode('utf-8', errors='ignore')
                    
                    # Read value type and value
                    value_type = struct.unpack('<I', f.read(4))[0]
                    value = self._read_value(f, value_type)
                    
                    metadata[key] = value
                
                self.metadata = metadata
                return metadata
        except Exception as e:
            print(f"Error parsing GGUF {self.filepath}: {e}")
            return None
    
    def _read_value(self, f, value_type: int):
        """Parse GGUF value type and read value"""
        type_map = {
            0: ('uint8', 1),
            1: ('int8', 1),
            2: ('uint16', 2),
            3: ('int16', 2),
            4: ('uint32', 4),
            5: ('int32', 4),
            6: ('float32', 4),
            7: ('float64', 8),
            8: ('bool', 1),
        }
        
        if value_type in type_map:
            dtype, size = type_map[value_type]
            return struct.unpack(f'<{dtype}', f.read(size))[0]
        elif value_type == 9:  # String
            str_len = struct.unpack('<I', f.read(4))[0]
            return f.read(str_len).decode('utf-8', errors='ignore')
        elif value_type == 10:  # Array - simplified handling
            array_type = struct.unpack('<I', f.read(4))[0]
            array_len = struct.unpack('<Q', f.read(8))[0]
            return f"[{array_type} array, len={array_len}]"
        
        return None
    
    def extract_model_info(self) -> Dict:
        """Extract model-relevant fields from GGUF metadata"""
        if not self.metadata:
            return {}
        
        info = {}
        
        # Model architecture
        if 'general.architecture' in self.metadata:
            info['architecture'] = self.metadata['general.architecture']
        
        # Model name
        if 'general.name' in self.metadata:
            info['model_name'] = self.metadata['general.name']
        
        # Context window
        context_keys = [
            f'{self.metadata.get("general.architecture", "llama")}.context_length',
            'llama.context_length',
            'mistral.context_length',
        ]
        for key in context_keys:
            if key in self.metadata:
                info['context_window'] = self.metadata[key]
                break
        
        # Quantization (inferred from tensors, but check metadata)
        if 'general.quantization_version' in self.metadata:
            info['quantization_type'] = self.metadata['general.quantization_version']
        
        # Check for attention type
        arch = self.metadata.get('general.architecture', '')
        if 'flash_attn' in str(self.metadata):
            info['attention_type'] = 'flash_attention'
        
        # RoPE scaling
        rope_scale_keys = [
            f'{self.metadata.get("general.architecture", "llama")}.rope_freq_scale',
            'llama.rope_freq_scale'
        ]
        for key in rope_scale_keys:
            if key in self.metadata:
                info['rope_freq_scale'] = self.metadata[key]
                break
        
        return info


class HuggingFaceMetadataFetcher:
    """Fetch model information from HuggingFace Hub"""
    
    def __init__(self):
        self.api = HfApi() if HF_AVAILABLE else None
    
    def find_model_id(self, model_name: str, filename: str) -> Optional[str]:
        """
        Attempt to identify HuggingFace model ID from filename/model name.
        Returns model_id or None if not found.
        """
        if not HF_AVAILABLE:
            return None
        
        # Extract potential model identifiers from filename
        # Format: organization/model-name-params or similar
        
        # Try exact match first
        try:
            model_info(model_name)
            return model_name
        except:
            pass
        
        # Try common patterns
        patterns = [
            # "model-7b-instruct" -> "author/model-7b-instruct"
            lambda x: f"meta-llama/{x}" if 'llama' in x.lower() else None,
            lambda x: f"mistralai/{x}" if 'mistral' in x.lower() else None,
            lambda x: f"01-ai/{x}" if 'yi' in x.lower() else None,
        ]
        
        for pattern in patterns:
            candidate = pattern(model_name)
            if candidate:
                try:
                    model_info(candidate)
                    return candidate
                except:
                    pass
        
        return None
    
    def fetch_metadata(self, model_id: str) -> Optional[Dict]:
        """
        Fetch metadata from HuggingFace model card and repository.
        Returns dict with available metadata.
        """
        if not HF_AVAILABLE:
            return None
        
        try:
            info = model_info(model_id)
            metadata = {
                'hf_model_id': model_id,
                'hf_url': f"https://huggingface.co/{model_id}",
                'description': info.description if hasattr(info, 'description') else None,
                'private': info.private,
                'downloads': info.downloads,
            }
            
            # Parse tags for capabilities
            if hasattr(info, 'tags'):
                metadata['tags'] = info.tags
                metadata['supports_reasoning'] = any('reasoning' in tag.lower() for tag in (info.tags or []))
                metadata['supports_tool_use'] = any('tool' in tag.lower() or 'function' in tag.lower() for tag in (info.tags or []))
                metadata['is_moe'] = any('moe' in tag.lower() or 'mixture' in tag.lower() for tag in (info.tags or []))
            
            return metadata
        except Exception as e:
            print(f"Error fetching HF metadata for {model_id}: {e}")
            return None


class ModelIndexBuilder:
    """Main orchestrator for building model index"""
    
    def __init__(self, models_dir: str):
        self.models_dir = Path(models_dir)
        self.gguf_parser = GGUFParser
        self.hf_fetcher = HuggingFaceMetadataFetcher()
        self.models: Dict[str, ModelMetadata] = {}
    
    def discover_models(self) -> List[Path]:
        """Find all GGUF files in models directory"""
        return sorted(self.models_dir.rglob('*.gguf'))
    
    def identify_split_models(self, files: List[Path]) -> Dict[str, List[Path]]:
        """
        Group split GGUF files (e.g., model-00001-of-00005.gguf).
        Returns dict mapping base name to list of file paths.
        """
        split_pattern = re.compile(r'^(.+?)-(\d{5})-of-(\d{5})\.gguf$')
        groups = {}
        
        for filepath in files:
            match = split_pattern.match(filepath.name)
            if match:
                base_name = match.group(1)
                if base_name not in groups:
                    groups[base_name] = []
                groups[base_name].append(filepath)
            else:
                # Single file model
                key = filepath.name
                groups[key] = [filepath]
        
        return groups
    
    def quantization_from_filename(self, filename: str) -> tuple[Optional[str], Optional[float]]:
        """
        Extract quantization info from filename.
        Returns (quantization_type, bits_per_weight) or (None, None)
        """
        patterns = {
            r'Q(\d)_K_M': ('Q_K_M', 4.0),
            r'Q(\d)_K_S': ('Q_K_S', 4.0),
            r'Q(\d)_0': ('Q', 4.0),
            r'Q(\d)_1': ('Q', 8.0),
            r'IQ(\d)': ('IQ', 2.0),
            r'GGML_TYPE_F32': ('F32', 32.0),
            r'GGML_TYPE_F16': ('F16', 16.0),
            r'GGML_TYPE_BF16': ('BF16', 16.0),
        }
        
        for pattern, (qtype, bits) in patterns.items():
            if re.search(pattern, filename, re.IGNORECASE):
                return (qtype, bits)
        
        return (None, None)
    
    def extract_params_from_metadata(self, metadata: Dict) -> Optional[int]:
        """
        Attempt to calculate parameter count from GGUF metadata.
        This is architecture-dependent and may not always work.
        """
        try:
            # Common architecture parameter keys
            if 'llama.embedding_length' in metadata and 'llama.feed_forward_length' in metadata:
                # Approximate for LLaMA: params ≈ 4 * d * h + other components
                # But actual calculation requires architecture knowledge
                return None  # Requires more sophisticated calculation
        except:
            pass
        
        return None
    
    def infer_params_from_filename(self, filename: str) -> Optional[int]:
        """
        Extract parameter count from filename patterns.
        Looks for patterns like "7b", "13b", "70b", etc.
        """
        patterns = [
            r'(\d+(?:\.\d+)?)[bgm]',  # 7b, 7.5b, 70b, etc.
            r'(\d+)b-',
            r'-(\d+)b-',
        ]
        
        for pattern in patterns:
            match = re.search(pattern, filename, re.IGNORECASE)
            if match:
                size_str = match.group(1)
                try:
                    size = float(size_str)
                    # Convert to actual parameter count
                    if size < 1000:  # Assume billions if under 1000
                        return int(size * 1e9)
                except:
                    pass
        
        return None
    
    def build_index(self, output_file: str = None) -> Dict[str, ModelMetadata]:
        """
        Main indexing pipeline.
        """
        print(f"Discovering models in {self.models_dir}...")
