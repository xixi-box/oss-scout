export PATH="$HOME/go-sdk-new/go/bin:$PATH"
cd /tmp/mvtest || exit 1
git fetch -q origin pull/54967/head:pr54967 2>&1 | tail -2
git checkout -q pr54967
git log --oneline -3
echo "=== build pkg/mlog ==="
cd pkg
go build ./mlog/ 2>&1 | tail -10
echo "=== vet pkg/mlog ==="
go vet ./mlog/ 2>&1 | tail -15
echo "=== test pkg/mlog ==="
go test ./mlog/ -count=1 2>&1 | tail -20
