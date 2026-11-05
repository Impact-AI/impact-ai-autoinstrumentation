// SPDX-License-Identifier: Apache-2.0

package main

import (
	"encoding/json"
	"os"
	"path/filepath"
	"strings"
	"testing"
)

const apache = "                                 Apache License\n                           Version 2.0, January 2004\n"

func write(t *testing.T, dir, name, text string) {
	t.Helper()
	if err := os.MkdirAll(filepath.Dir(filepath.Join(dir, name)), 0o755); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(filepath.Join(dir, name), []byte(text), 0o600); err != nil {
		t.Fatal(err)
	}
}

func TestDetectNamesTheCommonLicenses(t *testing.T) {
	cases := map[string]string{
		apache: "Apache-2.0",
		"MIT License\n\nPermission is hereby granted, free of charge, to any person obtaining a copy of this software": "MIT",
		"Redistribution and use in source and binary forms, with or without modification, are permitted provided that:\n" +
			"1. a\n2. b\n3. Neither the name of the copyright holder nor the names of its contributors may be used": "BSD-3-Clause",
		"Redistribution and use in source and binary forms, with or without modification, are permitted provided that:\n1. a\n2. b": "BSD-2-Clause",
		"Mozilla Public License Version 2.0\n==================================":                                                    "MPL-2.0",
		"Permission to use, copy, modify, and/or distribute this software for any purpose with or without fee is hereby granted":    "ISC",
		"something nobody has seen": "UNKNOWN",
	}
	for text, want := range cases {
		if got := detect(text); got != want {
			t.Errorf("detect(%.40q) = %s, want %s", text, got, want)
		}
	}
}

func TestLicenseFilesAreTheLicenseNoticeAndPatentsFilesOfTheModuleRoot(t *testing.T) {
	dir := t.TempDir()
	for _, name := range []string{"LICENSE", "LICENSE.md", "COPYING", "NOTICE", "PATENTS", "licence.txt", "main.go", "README.md", "sub/LICENSE"} {
		write(t, dir, name, "x")
	}
	got, err := licenseFiles(dir)
	if err != nil {
		t.Fatal(err)
	}
	want := []string{"COPYING", "LICENSE", "LICENSE.md", "NOTICE", "PATENTS", "licence.txt"}
	if strings.Join(got, ",") != strings.Join(want, ",") {
		t.Errorf("licenseFiles = %v, want %v", got, want)
	}
}

func TestCollectCopiesTheFilesOfEveryModuleAndTheGoLibrary(t *testing.T) {
	root, out := t.TempDir(), t.TempDir()
	mod := filepath.Join(root, "mod")
	write(t, mod, "LICENSE", apache)
	write(t, mod, "NOTICE", "a notice")
	goroot := filepath.Join(root, "goroot")
	write(t, goroot, "LICENSE", "Redistribution and use in source and binary forms, with or without modification, are permitted\nNeither the name of Google")
	write(t, goroot, "PATENTS", "Additional IP Rights Grant")

	entries, err := collect([]module{{Path: "example.com/lib", Version: "v1.2.3", Dir: mod}}, goroot, "go1.27.3", out)
	if err != nil {
		t.Fatal(err)
	}
	if len(entries) != 2 {
		t.Fatalf("entries = %+v", entries)
	}
	if entries[0].Name != "example.com/lib" || entries[0].License != "Apache-2.0" || strings.Join(entries[0].Files, ",") != "LICENSE,NOTICE" {
		t.Errorf("module entry = %+v", entries[0])
	}
	if entries[1].Name != "go (standard library and runtime)" || entries[1].Version != "go1.27.3" || entries[1].License != "BSD-3-Clause" {
		t.Errorf("go entry = %+v", entries[1])
	}
	for _, path := range []string{"example.com_lib@v1.2.3/LICENSE", "example.com_lib@v1.2.3/NOTICE", "go@go1.27.3/LICENSE", "go@go1.27.3/PATENTS"} {
		if _, err := os.Stat(filepath.Join(out, path)); err != nil {
			t.Errorf("missing %s: %v", path, err)
		}
	}
}

func TestAModuleWithoutALicenseFileFailsTheBuild(t *testing.T) {
	root, out := t.TempDir(), t.TempDir()
	write(t, filepath.Join(root, "mod"), "main.go", "package x")
	write(t, filepath.Join(root, "goroot"), "LICENSE", "x")
	_, err := collect([]module{{Path: "example.com/bare", Version: "v0.1.0", Dir: filepath.Join(root, "mod")}}, filepath.Join(root, "goroot"), "go1.27.3", out)
	if err == nil || !strings.Contains(err.Error(), "example.com/bare") {
		t.Fatalf("err = %v, want one naming the module", err)
	}
}

func TestTheIndexListsEveryEntryAsMarkdownAndJSON(t *testing.T) {
	entries := []entry{
		{Name: "example.com/lib", Version: "v1.2.3", License: "MIT", Files: []string{"LICENSE"}},
		{Name: "go (standard library and runtime)", Version: "go1.27.3", License: "BSD-3-Clause", Files: []string{"LICENSE", "PATENTS"}},
	}
	out := t.TempDir()
	if err := writeIndex(out, entries); err != nil {
		t.Fatal(err)
	}
	md, _ := os.ReadFile(filepath.Join(out, "INDEX.md"))
	if !strings.Contains(string(md), "| Module | Version | License | Files |") || !strings.Contains(string(md), "| example.com/lib | v1.2.3 | MIT | LICENSE |") {
		t.Errorf("INDEX.md = %s", md)
	}
	raw, _ := os.ReadFile(filepath.Join(out, "index.json"))
	var back []entry
	if err := json.Unmarshal(raw, &back); err != nil || len(back) != 2 || back[1].License != "BSD-3-Clause" {
		t.Errorf("index.json = %s (%v)", raw, err)
	}
}

func TestTheReporterLinksNoThirdPartyModule(t *testing.T) {
	// The reporter uses the standard library only. This fails the day a dependency is added, to say that its license
	// is now collected too (and that NOTICE needs a line for it).
	mods, err := linkedModules("../../cmd/reporter")
	if err != nil {
		t.Fatal(err)
	}
	if len(mods) != 0 {
		t.Errorf("third-party modules linked into the reporter: %+v", mods)
	}
}
