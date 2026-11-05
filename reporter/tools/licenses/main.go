// SPDX-License-Identifier: Apache-2.0

// Command licenses writes the license and notice files of what a Go program is built from, and an index of them.
//
//	go run ./tools/licenses -pkg ./cmd/reporter -out /out/licenses
//
// For every third-party module the program links in it copies the module's LICENSE, NOTICE and PATENTS files from the
// module cache, and it adds the Go runtime and standard library, which every Go binary contains. A module without a
// license file fails the build: it could not be redistributed. The index (INDEX.md and index.json) names each module,
// its version, the license the text shows and the files.
package main

import (
	"bytes"
	"encoding/json"
	"flag"
	"fmt"
	"os"
	"os/exec"
	"path/filepath"
	"regexp"
	"sort"
	"strings"
)

type module struct{ Path, Version, Dir string }

type entry struct {
	Name    string   `json:"name"`
	Version string   `json:"version"`
	License string   `json:"license"`
	Files   []string `json:"files"`
}

var licenseName = regexp.MustCompile(`(?i)^(licen[cs]e|copying|notice|patents)([-._].*)?$`)

func main() {
	pkg := flag.String("pkg", "./cmd/reporter", "the main package whose dependencies are listed")
	out := flag.String("out", "", "directory that receives the licenses and the index")
	flag.Parse()
	if *out == "" {
		fmt.Fprintln(os.Stderr, "licenses: -out is required")
		os.Exit(2)
	}
	if err := run(*pkg, *out); err != nil {
		fmt.Fprintln(os.Stderr, "licenses:", err)
		os.Exit(1)
	}
}

func run(pkg, out string) error {
	mods, err := linkedModules(pkg)
	if err != nil {
		return err
	}
	goroot, err := goEnv("GOROOT")
	if err != nil {
		return err
	}
	version, err := goEnv("GOVERSION")
	if err != nil {
		return err
	}
	entries, err := collect(mods, goroot, version, out)
	if err != nil {
		return err
	}
	return writeIndex(out, entries)
}

func goEnv(name string) (string, error) {
	out, err := exec.Command("go", "env", name).Output()
	if err != nil {
		return "", fmt.Errorf("go env %s: %w", name, err)
	}
	return strings.TrimSpace(string(out)), nil
}

// linkedModules lists the third-party modules that the packages of pkg are built from (the main module and the
// standard library are not among them).
func linkedModules(pkg string) ([]module, error) {
	cmd := exec.Command("go", "list", "-deps", "-f", `{{with .Module}}{{if not .Main}}{{.Path}}|{{.Version}}|{{.Dir}}{{end}}{{end}}`, pkg)
	var stderr bytes.Buffer
	cmd.Stderr = &stderr
	raw, err := cmd.Output()
	if err != nil {
		return nil, fmt.Errorf("go list -deps %s: %w\n%s", pkg, err, stderr.String())
	}
	seen := map[string]module{}
	for _, line := range strings.Split(string(raw), "\n") {
		parts := strings.Split(strings.TrimSpace(line), "|")
		if len(parts) == 3 && parts[0] != "" {
			seen[parts[0]] = module{Path: parts[0], Version: parts[1], Dir: parts[2]}
		}
	}
	mods := make([]module, 0, len(seen))
	for _, m := range seen {
		mods = append(mods, m)
	}
	sort.Slice(mods, func(i, j int) bool { return mods[i].Path < mods[j].Path })
	return mods, nil
}

// licenseFiles are the license, notice and patents files in the root of dir, sorted.
func licenseFiles(dir string) ([]string, error) {
	entries, err := os.ReadDir(dir)
	if err != nil {
		return nil, err
	}
	var names []string
	for _, e := range entries {
		if e.Type().IsRegular() && licenseName.MatchString(e.Name()) {
			names = append(names, e.Name())
		}
	}
	sort.Strings(names)
	return names, nil
}

func collect(mods []module, goroot, goVersion, out string) ([]entry, error) {
	var entries []entry
	add := func(name, version, dir, folder string) error {
		files, err := licenseFiles(dir)
		if err != nil {
			return err
		}
		if len(files) == 0 {
			return fmt.Errorf("%s %s has no license file in %s: it cannot be redistributed", name, version, dir)
		}
		target := filepath.Join(out, folder)
		if err := os.MkdirAll(target, 0o755); err != nil {
			return err
		}
		var texts []string
		for _, f := range files {
			data, err := os.ReadFile(filepath.Join(dir, f))
			if err != nil {
				return err
			}
			if err := os.WriteFile(filepath.Join(target, f), data, 0o644); err != nil {
				return err
			}
			if strings.HasPrefix(strings.ToUpper(f), "NOTICE") || strings.HasPrefix(strings.ToUpper(f), "PATENTS") {
				continue
			}
			texts = append(texts, string(data))
		}
		entries = append(entries, entry{Name: name, Version: version, License: detect(strings.Join(texts, "\n")), Files: files})
		return nil
	}
	for _, m := range mods {
		if err := add(m.Path, m.Version, m.Dir, strings.ReplaceAll(m.Path, "/", "_")+"@"+m.Version); err != nil {
			return nil, err
		}
	}
	// Every Go binary contains the runtime and the standard library.
	if err := add("go (standard library and runtime)", goVersion, goroot, "go@"+goVersion); err != nil {
		return nil, err
	}
	return entries, nil
}

var space = regexp.MustCompile(`\s+`)

// detect names a license from its text, for the common ones; UNKNOWN sends the reader to the files.
func detect(text string) string {
	t := space.ReplaceAllString(text, " ")
	switch {
	case strings.Contains(t, "Apache License") && strings.Contains(t, "Version 2.0"):
		return "Apache-2.0"
	case strings.Contains(t, "Mozilla Public License") && strings.Contains(t, "2.0"):
		return "MPL-2.0"
	case strings.Contains(t, "Permission is hereby granted, free of charge"):
		return "MIT"
	case strings.Contains(t, "Permission to use, copy, modify, and/or distribute this software for any purpose with or without fee"):
		return "ISC"
	case strings.Contains(t, "Redistribution and use in source and binary forms"):
		if strings.Contains(t, "Neither the name") || strings.Contains(t, "endorse or promote") {
			return "BSD-3-Clause"
		}
		return "BSD-2-Clause"
	}
	return "UNKNOWN"
}

func writeIndex(out string, entries []entry) error {
	if err := os.MkdirAll(out, 0o755); err != nil {
		return err
	}
	raw, err := json.MarshalIndent(entries, "", "  ")
	if err != nil {
		return err
	}
	if err := os.WriteFile(filepath.Join(out, "index.json"), append(raw, '\n'), 0o644); err != nil {
		return err
	}
	var b strings.Builder
	b.WriteString("# Go modules in this image\n\n")
	b.WriteString("The license and notice files of each are in the directory named after it, next to this index.\n")
	if len(entries) == 1 {
		b.WriteString("The reporter links the Go runtime and standard library and no third-party module.\n")
	}
	b.WriteString("\n")
	b.WriteString("| Module | Version | License | Files |\n|---|---|---|---|\n")
	for _, e := range entries {
		fmt.Fprintf(&b, "| %s | %s | %s | %s |\n", e.Name, e.Version, e.License, strings.Join(e.Files, ", "))
	}
	return os.WriteFile(filepath.Join(out, "INDEX.md"), []byte(b.String()), 0o644)
}
