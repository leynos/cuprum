//! A scoped writer callback must not transfer its handle back out.

use cuprum_native_io::with_owned_writer;

fn escape_writer<W>(writer: W, replacement: W) -> W {
    with_owned_writer(&(), writer, |_, scoped_writer| {
        std::mem::replace(scoped_writer, replacement)
    })
}

fn main() -> Result<(), std::io::Error> {
    let (_, writer) = cuprum_native_io::pipe()?;
    let (_, replacement) = cuprum_native_io::pipe()?;
    drop(escape_writer(writer, replacement));
    Ok(())
}
