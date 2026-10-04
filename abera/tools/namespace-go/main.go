// namespace-go rewrites trusted Go source literals, never runtime SQL. It also
// promotes affected constants and their constant dependents to variables. Run
// only on a reviewed checkout; the resulting ordinary Go diff is reviewable.
package main

import (
	"bytes"
	"flag"
	"fmt"
	"go/ast"
	"go/format"
	"go/parser"
	"go/token"
	"io/fs"
	"os"
	"path/filepath"
	"regexp"
	"strconv"
	"strings"
)

var database = regexp.MustCompile(`\bsignoz_(logs|traces|metrics|metadata|analytics|meter|audit)\b`)

type source struct {
	path, pkg string
	file      *ast.File
	imports   map[string]string
	changed   bool
}

func main() {
	root := flag.String("root", ".", "repository root")
	module := flag.String("module", "github.com/SigNoz/signoz", "module import path")
	helper := flag.String("helper", "github.com/SigNoz/signoz/pkg/abera/namespace", "namespace helper import")
	flag.Parse()
	if err := rewrite(*root, *module, *helper); err != nil {
		fmt.Fprintln(os.Stderr, err)
		os.Exit(1)
	}
}

func rewrite(root, module, helper string) error {
	fset := token.NewFileSet()
	var files []*source
	err := filepath.WalkDir(root, func(path string, entry fs.DirEntry, err error) error {
		if err != nil {
			return err
		}
		rel, err := filepath.Rel(root, path)
		if err != nil {
			return err
		}
		rel = filepath.ToSlash(rel)
		if entry.IsDir() {
			if rel != "." && (strings.HasPrefix(entry.Name(), ".") || entry.Name() == "node_modules" || entry.Name() == "vendor" || entry.Name() == "target" || entry.Name() == "abera" || rel == "ee" || rel == "cmd/enterprise" || rel == "pkg/abera" || rel == "internal/aberanamespace") {
				return filepath.SkipDir
			}
			return nil
		}
		if !strings.HasSuffix(path, ".go") || strings.HasSuffix(path, "_test.go") {
			return nil
		}
		file, err := parser.ParseFile(fset, path, nil, parser.ParseComments)
		if err != nil {
			return err
		}
		s := &source{path: path, pkg: module + "/" + filepath.ToSlash(filepath.Dir(rel)), file: file, imports: map[string]string{}}
		for _, imp := range file.Imports {
			p, _ := strconv.Unquote(imp.Path.Value)
			name := filepath.Base(p)
			if imp.Name != nil {
				name = imp.Name.Name
			}
			s.imports[name] = p
		}
		files = append(files, s)
		return nil
	})
	if err != nil {
		return err
	}
	// AST replacement avoids altering comments, identifiers, import paths or
	// arbitrary caller-supplied SQL. Existing Resolve calls remain unchanged.
	for _, s := range files {
		if strings.HasSuffix(module, "/signoz-otel-collector") && strings.HasSuffix(filepath.ToSlash(s.path), "/schema_migrator/table.go") {
			for _, decl := range s.file.Decls {
				fn, ok := decl.(*ast.FuncDecl)
				if !ok || fn.Name.Name != "ToSQL" || fn.Recv == nil {
					continue
				}
				typ, ok := fn.Recv.List[0].Type.(*ast.Ident)
				if !ok || typ.Name != "Distributed" {
					continue
				}
				already := false
				ast.Inspect(fn.Body, func(n ast.Node) bool {
					if sel, ok := n.(*ast.SelectorExpr); ok && sel.Sel.Name == "DataCluster" {
						already = true
					}
					return true
				})
				if already {
					continue
				}
				receiver := fn.Recv.List[0].Names[0].Name
				field := &ast.SelectorExpr{X: ast.NewIdent(receiver), Sel: ast.NewIdent("Cluster")}
				call := &ast.CallExpr{Fun: &ast.SelectorExpr{X: ast.NewIdent("aberanamespace"), Sel: ast.NewIdent("DataCluster")}, Args: []ast.Expr{field}}
				fn.Body.List = append([]ast.Stmt{&ast.AssignStmt{Lhs: []ast.Expr{field}, Tok: token.ASSIGN, Rhs: []ast.Expr{call}}}, fn.Body.List...)
				s.changed = true
			}
		}
		ast.Inspect(s.file, func(n ast.Node) bool {
			switch v := n.(type) {
			case *ast.ImportSpec:
				return false
			case *ast.CallExpr:
				if sel, ok := v.Fun.(*ast.SelectorExpr); ok {
					if id, ok := sel.X.(*ast.Ident); ok && id.Name == "aberanamespace" {
						return false
					}
				}
			case *ast.BasicLit:
				if v.Kind != token.STRING {
					return true
				}
				literal, err := strconv.Unquote(v.Value)
				if err == nil && strings.HasSuffix(module, "/signoz-otel-collector") {
					for _, old := range []string{"SETTINGS max_threads = 2", "SETTINGS max_threads = 8"} {
						if strings.Contains(literal, old) {
							v.Value = strings.ReplaceAll(v.Value, old, "SETTINGS max_threads = 1")
							s.changed = true
						}
					}
				}
				if err == nil && database.MatchString(literal) {
					// The printer preserves this expression verbatim; reparsing below
					// makes it a CallExpr before constant dependency analysis.
					v.Value = "aberanamespace.Resolve(" + v.Value + ")"
					s.changed = true
				}
			}
			return true
		})
		if s.changed {
			var buf bytes.Buffer
			if err := format.Node(&buf, fset, s.file); err != nil {
				return err
			}
			parsed, err := parser.ParseFile(fset, s.path, buf.Bytes(), parser.ParseComments)
			if err != nil {
				return err
			}
			s.file = parsed
			usesHelper := false
			ast.Inspect(s.file, func(n ast.Node) bool {
				if sel, ok := n.(*ast.SelectorExpr); ok {
					if id, ok := sel.X.(*ast.Ident); ok && id.Name == "aberanamespace" {
						usesHelper = true
					}
				}
				return true
			})
			if usesHelper && s.imports["aberanamespace"] != helper {
				imp := &ast.ImportSpec{Name: ast.NewIdent("aberanamespace"), Path: &ast.BasicLit{Kind: token.STRING, Value: strconv.Quote(helper)}}
				s.file.Decls = append([]ast.Decl{&ast.GenDecl{Tok: token.IMPORT, Specs: []ast.Spec{imp}}}, s.file.Decls...)
			}
		}
	}
	variables := map[string]bool{}
	for changed := true; changed; {
		changed = false
		for _, s := range files {
			var visitErr error
			top := map[*ast.GenDecl]bool{}
			extracted := map[*ast.GenDecl]*ast.GenDecl{}
			for _, decl := range s.file.Decls {
				if gen, ok := decl.(*ast.GenDecl); ok {
					top[gen] = true
				}
			}
			ast.Inspect(s.file, func(n ast.Node) bool {
				gen, ok := n.(*ast.GenDecl)
				if !ok || gen.Tok != token.CONST {
					return true
				}
				needsVar, hasIota := false, false
				promote := map[ast.Spec]bool{}
				for _, spec := range gen.Specs {
					ast.Inspect(spec, func(n ast.Node) bool {
						switch v := n.(type) {
						case *ast.Ident:
							hasIota = hasIota || v.Name == "iota"
							promote[spec] = promote[spec] || variables[s.pkg+"."+v.Name]
						case *ast.SelectorExpr:
							if id, ok := v.X.(*ast.Ident); ok {
								promote[spec] = promote[spec] || id.Name == "aberanamespace" || variables[s.imports[id.Name]+"."+v.Sel.Name]
							}
						}
						return true
					})
					needsVar = needsVar || promote[spec]
				}
				if !needsVar {
					return false
				}
				if hasIota {
					visitErr = fmt.Errorf("manual constant split required in %s: iota group", s.path)
					return false
				}
				var kept, moved []ast.Spec
				for _, spec := range gen.Specs {
					value := spec.(*ast.ValueSpec)
					if len(value.Values) == 0 {
						visitErr = fmt.Errorf("manual constant split required in %s: implicit expression", s.path)
						return false
					}
					if promote[spec] || !top[gen] {
						moved = append(moved, spec)
						for _, name := range value.Names {
							variables[s.pkg+"."+name.Name] = true
						}
					} else {
						kept = append(kept, spec)
					}
				}
				if len(kept) == 0 {
					gen.Tok = token.VAR
				} else {
					gen.Specs = kept
					extracted[gen] = &ast.GenDecl{Tok: token.VAR, Specs: moved}
				}
				s.changed, changed = true, true
				return false
			})
			if visitErr != nil {
				return visitErr
			}
			var decls []ast.Decl
			for _, decl := range s.file.Decls {
				decls = append(decls, decl)
				if gen, ok := decl.(*ast.GenDecl); ok && extracted[gen] != nil {
					decls = append(decls, extracted[gen])
				}
			}
			s.file.Decls = decls
		}
	}
	count := 0
	for _, s := range files {
		if !s.changed {
			continue
		}
		var buf bytes.Buffer
		if err := format.Node(&buf, fset, s.file); err != nil {
			return err
		}
		if err := os.WriteFile(s.path, buf.Bytes(), 0644); err != nil {
			return err
		}
		count++
	}
	fmt.Printf("Updated %d Go files with explicit namespace resolution.\n", count)
	return nil
}
