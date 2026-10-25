use std::io::Cursor;

const FIXTURE: &[u8] = include_bytes!("fixtures/clipboard-rgba.png");
const PIXELS: &[u8] = &[
    255, 0, 0, 255, 0, 255, 0, 255, // red, green
    0, 0, 255, 255, 255, 255, 255, 255, // blue, white
];

fn decode_rgba(bytes: &[u8]) -> (u32, u32, Vec<u8>) {
    let decoder = png::Decoder::new(Cursor::new(bytes));
    let mut reader = decoder.read_info().expect("decode PNG header");
    let mut pixels = vec![0; reader.output_buffer_size().expect("bounded PNG size")];
    let frame = reader.next_frame(&mut pixels).expect("decode PNG pixels");
    assert_eq!(frame.bit_depth, png::BitDepth::Eight);
    pixels.truncate(frame.buffer_size());
    let rgba = match frame.color_type {
        png::ColorType::Rgba => pixels,
        png::ColorType::Rgb => pixels
            .as_chunks::<3>()
            .0
            .iter()
            .flat_map(|rgb| [rgb[0], rgb[1], rgb[2], 255])
            .collect(),
        other => panic!("expected RGB or RGBA pixels, got {other:?}"),
    };
    (frame.width, frame.height, rgba)
}

#[test]
fn fixture_decodes_to_owned_rgba_colors() {
    assert_eq!(decode_rgba(FIXTURE), (2, 2, PIXELS.to_vec()));
}

#[cfg(target_os = "macos")]
#[test]
#[ignore = "reads only the fixture clipboard prepared on a GitHub-hosted macOS runner"]
fn native_clipboard_preserves_fixture_pixels() {
    assert_eq!(std::env::var("GITHUB_ACTIONS").as_deref(), Ok("true"));
    assert_eq!(std::env::var("RUNNER_OS").as_deref(), Ok("macOS"));
    assert_eq!(
        std::env::var("RUNNER_ENVIRONMENT").as_deref(),
        Ok("github-hosted")
    );
    let actual =
        remote_agent_rs::local::read_clipboard_png().expect("read native fixture clipboard");
    assert_eq!(decode_rgba(&actual), decode_rgba(FIXTURE));
}
