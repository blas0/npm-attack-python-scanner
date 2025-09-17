#!/usr/bin/env python3
"""
Package Scanner - Searches for specific npm packages across the computer
"""

import os
import json
import re
import sys
from pathlib import Path
from typing import Set, Dict, List, Tuple
import argparse
from concurrent.futures import ThreadPoolExecutor, as_completed
import threading

class PackageScanner:
    def __init__(self, target_packages: Set[str]):
        self.target_packages = target_packages
        self.found_packages = {}
        self.lock = threading.Lock()
        self.scanned_dirs = set()
        
    def parse_package_list(self, package_text: str) -> Set[str]:
        """Parse the package list from the provided text"""
        packages = set()
        lines = package_text.strip().split('\n')
        
        for line in lines[1:]:  # Skip header
            if '\t' in line:
                package_name = line.split('\t')[0].strip()
                if package_name:
                    packages.add(package_name)
        
        return packages
    
    def is_node_modules_dir(self, path: Path) -> bool:
        """Check if a directory is a node_modules folder"""
        return path.name == 'node_modules' and path.is_dir()
    
    def should_skip_directory(self, path: Path) -> bool:
        """Check if we should skip scanning this directory"""
        skip_dirs = {
            '.git', '.svn', '.hg', '__pycache__', 
            'venv', 'env', '.env', 'virtualenv',
            '.vscode', '.idea', 'dist', 'build',
            'target', 'bin', 'obj', '.cache',
            'Windows', 'System32', 'Program Files',
            'Applications'  # macOS
        }
        
        return (
            path.name.startswith('.') and path.name not in {'.npm', '.node_modules'} or
            path.name in skip_dirs or
            not os.access(path, os.R_OK)
        )
    
    def scan_node_modules(self, node_modules_path: Path) -> Dict[str, str]:
        """Scan a node_modules directory for target packages"""
        found_in_this_dir = {}
        
        try:
            if not node_modules_path.exists():
                return found_in_this_dir
                
            for item in node_modules_path.iterdir():
                if not item.is_dir():
                    continue
                    
                # Handle scoped packages (e.g., @angular/core)
                if item.name.startswith('@'):
                    try:
                        for scoped_package in item.iterdir():
                            if scoped_package.is_dir():
                                full_name = f"{item.name}/{scoped_package.name}"
                                if full_name in self.target_packages:
                                    version = self.get_package_version(scoped_package)
                                    found_in_this_dir[full_name] = {
                                        'version': version,
                                        'path': str(scoped_package),
                                        'parent_project': self.find_parent_project(node_modules_path)
                                    }
                    except (PermissionError, OSError):
                        continue
                else:
                    # Regular packages
                    if item.name in self.target_packages:
                        version = self.get_package_version(item)
                        found_in_this_dir[item.name] = {
                            'version': version,
                            'path': str(item),
                            'parent_project': self.find_parent_project(node_modules_path)
                        }
                        
        except (PermissionError, OSError) as e:
            print(f"Warning: Cannot access {node_modules_path}: {e}")
            
        return found_in_this_dir
    
    def get_package_version(self, package_path: Path) -> str:
        """Get the version of a package from its package.json"""
        try:
            package_json_path = package_path / 'package.json'
            if package_json_path.exists():
                with open(package_json_path, 'r', encoding='utf-8') as f:
                    package_data = json.load(f)
                    return package_data.get('version', 'unknown')
        except (json.JSONDecodeError, PermissionError, OSError):
            pass
        return 'unknown'
    
    def find_parent_project(self, node_modules_path: Path) -> str:
        """Find the parent project name by looking for package.json in parent directory"""
        try:
            parent_dir = node_modules_path.parent
            package_json = parent_dir / 'package.json'
            
            if package_json.exists():
                with open(package_json, 'r', encoding='utf-8') as f:
                    data = json.load(f)
                    return data.get('name', str(parent_dir.name))
            else:
                return str(parent_dir.name)
        except (json.JSONDecodeError, PermissionError, OSError):
            return str(node_modules_path.parent.name)
    
    def scan_package_json(self, package_json_path: Path) -> Dict[str, str]:
        """Scan a package.json file for target packages in dependencies"""
        found_dependencies = {}
        
        try:
            with open(package_json_path, 'r', encoding='utf-8') as f:
                data = json.load(f)
                
            # Check all dependency sections
            dep_sections = ['dependencies', 'devDependencies', 'peerDependencies', 'optionalDependencies']
            
            for section in dep_sections:
                if section in data:
                    for pkg_name, version in data[section].items():
                        if pkg_name in self.target_packages:
                            found_dependencies[pkg_name] = {
                                'version': version,
                                'dependency_type': section,
                                'project': data.get('name', 'unknown'),
                                'path': str(package_json_path)
                            }
                            
        except (json.JSONDecodeError, PermissionError, OSError) as e:
            print(f"Warning: Cannot read {package_json_path}: {e}")
            
        return found_dependencies
    
    def scan_directory_worker(self, root_path: Path) -> Tuple[Dict, Dict]:
        """Worker function to scan a directory tree"""
        installed_packages = {}
        dependency_references = {}
        
        try:
            for current_path, dirs, files in os.walk(root_path):
                current_path = Path(current_path)
                
                # Skip if we should avoid this directory
                if self.should_skip_directory(current_path):
                    dirs.clear()  # Don't recurse into subdirectories
                    continue
                
                # Avoid scanning the same node_modules twice
                path_str = str(current_path)
                if path_str in self.scanned_dirs:
                    continue
                self.scanned_dirs.add(path_str)
                
                # Check for node_modules directory
                if current_path.name == 'node_modules':
                    found = self.scan_node_modules(current_path)
                    installed_packages.update(found)
                    dirs.clear()  # Don't recurse further into node_modules
                    continue
                
                # Check for package.json files
                if 'package.json' in files:
                    package_json_path = current_path / 'package.json'
                    found_deps = self.scan_package_json(package_json_path)
                    dependency_references.update(found_deps)
                
                # Limit recursion depth and skip deep nested structures
                if len(current_path.parts) > 10:
                    dirs.clear()
                    
        except Exception as e:
            print(f"Error scanning {root_path}: {e}")
            
        return installed_packages, dependency_references
    
    def scan_computer(self, max_workers: int = 4) -> Dict:
        """Scan the entire computer for target packages"""
        results = {
            'installed_packages': {},
            'dependency_references': {},
            'summary': {}
        }
        
        # Get common search locations
        search_paths = self.get_search_paths()
        
        print(f"Scanning {len(search_paths)} locations for {len(self.target_packages)} packages...")
        print("This may take several minutes depending on your system size.")
        
        # Use ThreadPoolExecutor for parallel scanning
        with ThreadPoolExecutor(max_workers=max_workers) as executor:
            future_to_path = {
                executor.submit(self.scan_directory_worker, path): path 
                for path in search_paths
            }
            
            completed = 0
            for future in as_completed(future_to_path):
                path = future_to_path[future]
                try:
                    installed, dependencies = future.result()
                    results['installed_packages'].update(installed)
                    results['dependency_references'].update(dependencies)
                    
                    completed += 1
                    print(f"Progress: {completed}/{len(search_paths)} locations scanned")
                    
                except Exception as e:
                    print(f"Error processing {path}: {e}")
        
        # Generate summary
        results['summary'] = {
            'total_target_packages': len(self.target_packages),
            'installed_packages_found': len(results['installed_packages']),
            'dependency_references_found': len(results['dependency_references']),
            'unique_packages_found': len(set(results['installed_packages'].keys()) | 
                                       set(results['dependency_references'].keys()))
        }
        
        return results
    
    def get_search_paths(self) -> List[Path]:
        """Get list of paths to search"""
        paths = []
        
        # User home directory
        home = Path.home()
        paths.append(home)
        
        # Common development directories
        common_dev_dirs = [
            home / 'Documents',
            home / 'Desktop',
            home / 'Projects',
            home / 'Development',
            home / 'dev',
            home / 'workspace',
            home / 'code'
        ]
        
        for dev_dir in common_dev_dirs:
            if dev_dir.exists() and dev_dir.is_dir():
                paths.append(dev_dir)
        
        # Global npm directories
        if sys.platform.startswith('win'):
            # Windows paths
            global_npm_paths = [
                Path(os.environ.get('APPDATA', '')) / 'npm',
                Path(os.environ.get('PROGRAMFILES', '')) / 'nodejs',
                Path(os.environ.get('PROGRAMFILES(X86)', '')) / 'nodejs',
            ]
        else:
            # Unix-like paths
            global_npm_paths = [
                Path('/usr/local/lib/node_modules'),
                Path('/usr/lib/node_modules'),
                home / '.npm',
                home / '.npm-global',
                Path('/opt/homebrew/lib/node_modules') if sys.platform == 'darwin' else None
            ]
        
        for npm_path in global_npm_paths:
            if npm_path and npm_path.exists():
                paths.append(npm_path)
        
        return [p for p in paths if p.exists()]
    
    def print_results(self, results: Dict):
        """Print the scan results in a readable format"""
        print("\n" + "="*80)
        print("PACKAGE SCAN RESULTS")
        print("="*80)
        
        print(f"\nSUMMARY:")
        print(f"Target packages: {results['summary']['total_target_packages']}")
        print(f"Installed packages found: {results['summary']['installed_packages_found']}")
        print(f"Dependency references found: {results['summary']['dependency_references_found']}")
        print(f"Unique packages found: {results['summary']['unique_packages_found']}")
        
        if results['installed_packages']:
            print(f"\nINSTALLED PACKAGES ({len(results['installed_packages'])}):")
            print("-" * 40)
            for pkg_name, info in sorted(results['installed_packages'].items()):
                print(f"{pkg_name} (v{info['version']})")
                print(f"  Path: {info['path']}")
                print(f"  Project: {info['parent_project']}")
                print()
        
        if results['dependency_references']:
            print(f"\nDEPENDENCY REFERENCES ({len(results['dependency_references'])}):")
            print("-" * 40)
            for pkg_name, info in sorted(results['dependency_references'].items()):
                print(f"{pkg_name} (v{info['version']}) - {info['dependency_type']}")
                print(f"  Project: {info['project']}")
                print(f"  Path: {info['path']}")
                print()
        
        if not results['installed_packages'] and not results['dependency_references']:
            print("\nNo target packages found on this system.")


def main():
    # Package list from the provided document
    PACKAGE_LIST = """Package	Versions
@ahmedhfarag/ngx-perfect-scrollbar	20.0.20
@ahmedhfarag/ngx-virtual-scroller	4.0.4
@art-ws/common	2.0.28
@art-ws/config-eslint	2.0.4, 2.0.5
@art-ws/config-ts	2.0.7, 2.0.8
@art-ws/db-context	2.0.24
@art-ws/di	2.0.28, 2.0.32
@art-ws/di-node	2.0.13
@art-ws/eslint	1.0.5, 1.0.6
@art-ws/fastify-http-server	2.0.24, 2.0.27
@art-ws/http-server	2.0.21, 2.0.25
@art-ws/openapi	0.1.9, 0.1.12
@art-ws/package-base	1.0.5, 1.0.6
@art-ws/prettier	1.0.5, 1.0.6
@art-ws/slf	2.0.15, 2.0.22
@art-ws/ssl-info	1.0.9, 1.0.10
@art-ws/web-app	1.0.3, 1.0.4
@crowdstrike/commitlint	8.1.1, 8.1.2
@crowdstrike/falcon-shoelace	0.4.1, 0.4.2
@crowdstrike/foundry-js	0.19.1, 0.19.2
@crowdstrike/glide-core	0.34.2, 0.34.3
@crowdstrike/logscale-dashboard	1.205.1, 1.205.2
@crowdstrike/logscale-file-editor	1.205.1, 1.205.2
@crowdstrike/logscale-parser-edit	1.205.1, 1.205.2
@crowdstrike/logscale-search	1.205.1, 1.205.2
@crowdstrike/tailwind-toucan-base	5.0.1, 5.0.2
@ctrl/deluge	7.2.1, 7.2.2
@ctrl/golang-template	1.4.2, 1.4.3
@ctrl/magnet-link	4.0.3, 4.0.4
@ctrl/ngx-codemirror	7.0.1, 7.0.2
@ctrl/ngx-csv	6.0.1, 6.0.2
@ctrl/ngx-emoji-mart	9.2.1, 9.2.2
@ctrl/ngx-rightclick	4.0.1, 4.0.2
@ctrl/qbittorrent	9.7.1, 9.7.2
@ctrl/react-adsense	2.0.1, 2.0.2
@ctrl/shared-torrent	6.3.1, 6.3.2
@ctrl/tinycolor	4.1.1, 4.1.2
@ctrl/torrent-file	4.1.1, 4.1.2
@ctrl/transmission	7.3.1
@ctrl/ts-base32	4.0.1, 4.0.2
@hestjs/core	0.2.1
@hestjs/cqrs	0.1.6
@hestjs/demo	0.1.2
@hestjs/eslint-config	0.1.2
@hestjs/logger	0.1.6
@hestjs/scalar	0.1.7
@hestjs/validation	0.1.6
@nativescript-community/arraybuffers	1.1.6, 1.1.7, 1.1.8
@nativescript-community/gesturehandler	2.0.35
@nativescript-community/perms	3.0.5, 3.0.6, 3.0.7, 3.0.8, 3.0.9
@nativescript-community/sqlite	3.5.2, 3.5.3, 3.5.4, 3.5.5
@nativescript-community/text	1.6.9, 1.6.10, 1.6.11, 1.6.12, 1.6.13
@nativescript-community/typeorm	0.2.30, 0.2.31, 0.2.32, 0.2.33
@nativescript-community/ui-collectionview	6.0.6
@nativescript-community/ui-document-picker	1.1.27, 1.1.28
@nativescript-community/ui-drawer	0.1.30
@nativescript-community/ui-image	4.5.6
@nativescript-community/ui-label	1.3.35, 1.3.36, 1.3.37
@nativescript-community/ui-material-bottom-navigation	7.2.72, 7.2.73, 7.2.74, 7.2.75
@nativescript-community/ui-material-bottomsheet	7.2.72
@nativescript-community/ui-material-core	7.2.72, 7.2.73, 7.2.74, 7.2.75, 7.2.76
@nativescript-community/ui-material-core-tabs	7.2.72, 7.2.73, 7.2.74, 7.2.75, 7.2.76
@nativescript-community/ui-material-ripple	7.2.72, 7.2.73, 7.2.74, 7.2.75
@nativescript-community/ui-material-tabs	7.2.72, 7.2.73, 7.2.74, 7.2.75
@nativescript-community/ui-pager	14.1.36, 14.1.37, 14.1.38, 14.1.35
@nativescript-community/ui-pulltorefresh	2.5.4, 2.5.5, 2.5.6, 2.5.7
@nexe/config-manager	0.1.1
@nexe/eslint-config	0.1.1
@nexe/logger	0.1.3
@nstudio/angular	20.0.4, 20.0.5, 20.0.6
@nstudio/focus	20.0.4, 20.0.5, 20.0.6
@nstudio/nativescript-checkbox	2.0.6, 2.0.7, 2.0.8, 2.0.9
@nstudio/nativescript-loading-indicator	5.0.1, 5.0.2, 5.0.3, 5.0.4
@nstudio/ui-collectionview	5.1.11, 5.1.12, 5.1.13, 5.1.14
@nstudio/web	20.0.4
@nstudio/web-angular	20.0.4
@nstudio/xplat	20.0.5, 20.0.6, 20.0.7, 20.0.4
@nstudio/xplat-utils	20.0.5, 20.0.6, 20.0.7, 20.0.4
@operato/board	9.0.36, 9.0.37, 9.0.38, 9.0.39, 9.0.40, 9.0.41, 9.0.42, 9.0.43, 9.0.44, 9.0.45, 9.0.46
@operato/data-grist	9.0.29, 9.0.35, 9.0.36, 9.0.37
@operato/graphql	9.0.22, 9.0.35, 9.0.36, 9.0.37, 9.0.38, 9.0.39, 9.0.40, 9.0.41, 9.0.42, 9.0.43, 9.0.44, 9.0.45, 9.0.46
@operato/headroom	9.0.2, 9.0.35, 9.0.36, 9.0.37
@operato/help	9.0.35, 9.0.36, 9.0.37, 9.0.38, 9.0.39, 9.0.40, 9.0.41, 9.0.42, 9.0.43, 9.0.44, 9.0.45, 9.0.46
@operato/i18n	9.0.35, 9.0.36, 9.0.37
@operato/input	9.0.27, 9.0.35, 9.0.36, 9.0.37, 9.0.38, 9.0.39, 9.0.40, 9.0.41, 9.0.42, 9.0.43, 9.0.44, 9.0.45, 9.0.46
@operato/layout	9.0.35, 9.0.36, 9.0.37
@operato/popup	9.0.22, 9.0.35, 9.0.36, 9.0.37, 9.0.38, 9.0.39, 9.0.40, 9.0.41, 9.0.42, 9.0.43, 9.0.44, 9.0.45, 9.0.46
@operato/pull-to-refresh	9.0.36, 9.0.37, 9.0.38, 9.0.39, 9.0.40, 9.0.41, 9.0.42
@operato/shell	9.0.22, 9.0.35, 9.0.36, 9.0.37, 9.0.38, 9.0.39
@operato/styles	9.0.2, 9.0.35, 9.0.36, 9.0.37
@operato/utils	9.0.22, 9.0.35, 9.0.36, 9.0.37, 9.0.38, 9.0.39, 9.0.40, 9.0.41, 9.0.42, 9.0.43, 9.0.44, 9.0.45, 9.0.46
@teselagen/bounce-loader	0.3.16, 0.3.17
@teselagen/liquibase-tools	0.4.1
@teselagen/range-utils	0.3.14, 0.3.15
@teselagen/react-list	0.8.19, 0.8.20
@teselagen/react-table	6.10.19, 6.10.21
@thangved/callback-window	1.1.4
@things-factory/attachment-base	9.0.43, 9.0.44, 9.0.45, 9.0.46, 9.0.47, 9.0.48, 9.0.49, 9.0.50
@things-factory/auth-base	9.0.43, 9.0.44, 9.0.45
@things-factory/email-base	9.0.42, 9.0.43, 9.0.44, 9.0.45, 9.0.46, 9.0.47, 9.0.48, 9.0.49, 9.0.50, 9.0.51, 9.0.52, 9.0.53, 9.0.54
@things-factory/env	9.0.42, 9.0.43, 9.0.44, 9.0.45
@things-factory/integration-base	9.0.43, 9.0.44, 9.0.45
@things-factory/integration-marketplace	9.0.43, 9.0.44, 9.0.45
@things-factory/shell	9.0.43, 9.0.44, 9.0.45
@tnf-dev/api	1.0.8
@tnf-dev/core	1.0.8
@tnf-dev/js	1.0.8
@tnf-dev/mui	1.0.8
@tnf-dev/react	1.0.8
@ui-ux-gang/devextreme-angular-rpk	24.1.7
@yoobic/design-system	6.5.17
@yoobic/jpeg-camera-es6	1.0.13
@yoobic/yobi	8.7.53
airchief	0.3.1
airpilot	0.8.8
angulartics2	14.1.1, 14.1.2
browser-webdriver-downloader	3.0.8
capacitor-notificationhandler	0.0.2, 0.0.3
capacitor-plugin-healthapp	0.0.2, 0.0.3
capacitor-plugin-ihealth	1.1.8, 1.1.9
capacitor-plugin-vonage	1.0.2, 1.0.3
capacitorandroidpermissions	0.0.4, 0.0.5
config-cordova	0.8.5
cordova-plugin-voxeet2	1.0.24
cordova-voxeet	1.0.32
create-hest-app	0.1.9
db-evo	1.1.4, 1.1.5
devextreme-angular-rpk	21.2.8
ember-browser-services	5.0.2, 5.0.3
ember-headless-form	1.1.2, 1.1.3
ember-headless-form-yup	1.0.1
ember-headless-table	2.1.5, 2.1.6
ember-url-hash-polyfill	1.0.12, 1.0.13
ember-velcro	2.2.1, 2.2.2
encounter-playground	0.0.2, 0.0.3, 0.0.4, 0.0.5
eslint-config-crowdstrike	11.0.2, 11.0.3
eslint-config-crowdstrike-node	4.0.3, 4.0.4
eslint-config-teselagen	6.1.7
globalize-rpk	1.7.4
graphql-sequelize-teselagen	5.3.8
html-to-base64-image	1.0.2
json-rules-engine-simplified	0.2.1, 0.2.4, 0.2.3, 0.2.2
jumpgate	0.0.2
koa2-swagger-ui	5.11.1, 5.11.2
mcfly-semantic-release	1.3.1
mcp-knowledge-base	0.0.2
mcp-knowledge-graph	1.2.1
mobioffice-cli	1.0.3
monorepo-next	13.0.1, 13.0.2
mstate-angular	0.4.4
mstate-cli	0.4.7
mstate-dev-react	1.1.1
mstate-react	1.6.5
ng2-file-upload	7.0.2, 7.0.3, 8.0.1, 8.0.2, 8.0.3, 9.0.1
ngx-bootstrap	18.1.4, 19.0.3, 19.0.4, 20.0.3, 20.0.4, 20.0.5, 20.0.6
ngx-color	10.0.1, 10.0.2
ngx-toastr	19.0.1, 19.0.2
ngx-trend	8.0.1
ngx-ws	1.1.5, 1.1.6
oradm-to-gql	35.0.14, 35.0.15
oradm-to-sqlz	1.1.2, 1.1.3, 1.1.4
ove-auto-annotate	0.0.9
pm2-gelf-json	1.0.4, 1.0.5
printjs-rpk	1.6.1
react-complaint-image	0.0.32, 0.0.33, 0.0.34, 0.0.35
react-jsonschema-form-conditionals	0.3.18, 0.3.19, 0.3.20, 0.3.21
remark-preset-lint-crowdstrike	4.0.1, 4.0.2
rxnt-authentication	0.0.3, 0.0.4, 0.0.5, 0.0.6
rxnt-healthchecks-nestjs	1.0.2, 1.0.3, 1.0.4, 1.0.5
rxnt-kue	1.0.4, 1.0.5, 1.0.6, 1.0.7
swc-plugin-component-annotate	1.9.1, 1.9.2
tbssnch	1.0.2
teselagen-interval-tree	1.1.2
tg-client-query-builder	2.14.4, 2.14.5
tg-redbird	1.3.1
tg-seq-gen	1.0.9, 1.0.10
thangved-react-grid	1.0.3
ts-gaussian	3.0.5, 3.0.6
ts-imports	1.0.1, 1.0.2
tvi-cli	0.1.5
ve-bamreader	0.2.6
ve-editor	1.0.1
verror-extra	6.0.1
voip-callkit	1.0.2, 1.0.3
wdio-web-reporter	0.1.3
yargs-help-output	5.0.3
yoo-styles	6.0.326"""
    
    parser = argparse.ArgumentParser(description='Scan computer for specific npm packages')
    parser.add_argument('--threads', type=int, default=4, help='Number of worker threads (default: 4)')
    parser.add_argument('--output', type=str, help='Output results to JSON file')
    args = parser.parse_args()
    
    # Create scanner and parse packages
    scanner = PackageScanner(set())
    target_packages = scanner.parse_package_list(PACKAGE_LIST)
    scanner.target_packages = target_packages
    
    print(f"Starting scan for {len(target_packages)} packages...")
    
    try:
        results = scanner.scan_computer(max_workers=args.threads)
        scanner.print_results(results)
        
        if args.output:
            with open(args.output, 'w', encoding='utf-8') as f:
                json.dump(results, f, indent=2, default=str)
            print(f"\nResults saved to: {args.output}")
            
    except KeyboardInterrupt:
        print("\nScan interrupted by user.")
    except Exception as e:
        print(f"\nError during scan: {e}")
        sys.exit(1)


if __name__ == "__main__":
    main()
